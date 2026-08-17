"""Incremental ingestion — design §5.2.

Runs out of the request path, which is the whole point: the receiver enqueues and
returns so the instructor's Publish button never waits on our vendors.

The pipeline, in order, and each step exists for a stated reason:

    RESOLVE TO LEAVES  the event names the published CONTAINER, not the changed
                       leaves, so we walk descendants ourselves
    VALIDATE           supported block type? else skip and log, never guess
    DEDUP              already indexed usage_key@version? skip
    EXTRACT            in-platform, because modulestore is a Python API
    POST               the service chunks, embeds, writes and swaps

Failure handling is not optional here (Principle 8): retry with backoff on
transient errors, a permanent failure recorded in `failed_ingestions`, and every
failure logged with its usage_key. A block that fails all retries must be
*detectable* by the reconciliation sweep, never a silent gap.
"""

from __future__ import annotations

import logging

from celery import shared_task
from opaque_keys.edx.keys import UsageKey

log = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
    acks_late=True,
)
def ingest_published_block(self, usage_key: str, version: str, trace_id: str):
    """Resolve a published container to leaves and send them to the service."""
    from django.conf import settings

    from ..adapters import content_adapter
    from ..client.endpoints import active_version, prune_blocks, send_leaves
    from ..models import FailedIngestion

    key = UsageKey.from_string(usage_key)
    offering_id = str(key.course_key)

    # The adapter opens the published-branch context itself. It is thread-local
    # and defaults to None, so a Celery worker inherits nothing — which is
    # exactly why the adapter owns it rather than trusting this call site.
    leaves = list(content_adapter.iter_leaves(key))
    if not leaves:
        log.info("coursemate: no supported leaves under %s", usage_key)
        return {"blocks": 0}

    # --- the run lifecycle, which this task used to omit entirely -------------
    #
    # `write_chunks` writes every row INACTIVE. Something must then activate it,
    # and there are exactly two ways: `topup=True` activates the named blocks in
    # place, or `is_final=True` verifies the run and flips the whole course onto
    # it. This task passed neither, so a publish wrote chunks that were never
    # activated — the task logged success, `swapped=False` came back unread, and
    # the content stayed invisible until the nightly sweep noticed it missing and
    # repaired it. Up to a day of staleness from the one path whose entire
    # purpose is that an instructor's Publish reaches the tutor promptly.
    #
    # **`is_final` would be catastrophic here and must never be used on this
    # path.** `swap()` begins with
    #     UPDATE chunks SET active=0 WHERE offering_id=? AND version<>?
    # so finalising a run that contains only the published subtree deactivates
    # the entire rest of the course. The bug was serving stale content; that
    # "fix" would serve almost none.
    #
    # Top-up is the mechanism the sweep already uses for exactly this shape —
    # activate these blocks, leave the pointer alone — so this reuses it rather
    # than inventing a third lifecycle.
    active = active_version(offering_id)
    if not active:
        # Never indexed. A top-up would write rows under a version nothing points
        # at, and a publish must not bootstrap a course — that is a deliberate
        # operator action, the same rule the sweep applies.
        log.warning(
            "coursemate: %s has no active index version; skipping publish top-up "
            "for %s — run coursemate_bootstrap", offering_id, usage_key,
        )
        return {"blocks": 0, "skipped": "no active version"}

    # Re-publishing the same block must not leave two copies. `write_chunks` does
    # not deduplicate, so without this a block republished twice is written twice
    # under the active version, activated twice, and cited twice. Same reasoning
    # and same call the sweep makes before it re-ingests.
    prune_blocks(offering_id=offering_id, usage_keys=[leaf.usage_key for leaf in leaves])

    meta = content_adapter.get_course_meta(key.course_key)
    batch = settings.COURSEMATE_INGEST_BATCH_SIZE

    written = 0
    for start in range(0, len(leaves), batch):
        window = leaves[start : start + batch]
        try:
            result = send_leaves(
                tenant=settings.COURSEMATE_TENANT,
                course_id=offering_id,
                offering_id=offering_id,
                course_version=meta.get("course_version"),
                leaves=window,
                trace_id=trace_id,
                run_id=active,     # live and die with the version being served
                is_final=False,    # never: see above, swap would empty the course
                topup=True,        # activate just these blocks; do not re-swap
            )
            written += result.blocks_written
            for failed in result.failed:
                FailedIngestion.record(offering_id, failed, version, "service rejected")
        except Exception as exc:
            for leaf in window:
                FailedIngestion.record(
                    offering_id, leaf.usage_key, leaf.version, str(exc)
                )
            raise

    log.info(
        "coursemate: ingested %s/%s leaves under %s into active version %s",
        written, len(leaves), usage_key, active,
    )
    return {"blocks": written}


@shared_task(bind=True, max_retries=3, acks_late=True)
def delete_block(self, usage_key: str, trace_id: str):
    """XBLOCK_DELETED — drop every chunk under this subtree."""
    from django.conf import settings

    from ..client.endpoints import send_delete

    key = UsageKey.from_string(usage_key)
    send_delete(
        tenant=settings.COURSEMATE_TENANT,
        offering_id=str(key.course_key),
        usage_key=usage_key,
        trace_id=trace_id,
    )
    return {"deleted": usage_key}
