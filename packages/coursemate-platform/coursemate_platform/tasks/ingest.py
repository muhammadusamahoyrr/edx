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
    from ..client.endpoints import send_leaves
    from ..models import FailedIngestion

    key = UsageKey.from_string(usage_key)

    # The adapter opens the published-branch context itself. It is thread-local
    # and defaults to None, so a Celery worker inherits nothing — which is
    # exactly why the adapter owns it rather than trusting this call site.
    leaves = list(content_adapter.iter_leaves(key))
    if not leaves:
        log.info("coursemate: no supported leaves under %s", usage_key)
        return {"blocks": 0}

    meta = content_adapter.get_course_meta(key.course_key)
    batch = settings.COURSEMATE_INGEST_BATCH_SIZE

    written = 0
    for start in range(0, len(leaves), batch):
        window = leaves[start : start + batch]
        try:
            result = send_leaves(
                tenant=settings.COURSEMATE_TENANT,
                course_id=str(key.course_key),
                offering_id=str(key.course_key),
                course_version=meta.get("course_version"),
                leaves=window,
                trace_id=trace_id,
            )
            written += result.blocks_written
            for failed in result.failed:
                FailedIngestion.record(str(key.course_key), failed, version, "service rejected")
        except Exception as exc:
            for leaf in window:
                FailedIngestion.record(
                    str(key.course_key), leaf.usage_key, leaf.version, str(exc)
                )
            raise

    log.info("coursemate: ingested %s/%s leaves under %s", written, len(leaves), usage_key)
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
