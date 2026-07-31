"""Reconciliation sweep — design §5.4.

**Why this exists at all.** There is **no unpublish event** in `openedx-events`.
Publish, delete, duplicate, import and rerun all fire; unpublish does not. So if
an instructor unpublishes a unit, nothing tells us, and the tutor keeps answering
from — and citing — content students can no longer see. That is a direct
Principle 3 violation ("the AI only learns from published, authorized content")
and the kind of thing a reviewer will simply *try*.

No event can catch it, so the only mitigation is to periodically compare what we
serve against what the platform actually publishes:

    live     = published leaves, read through content_adapter
    indexed  = blocks the service currently serves
    orphans  = indexed - live   → remove (unpublished or deleted)
    missing  = live - indexed   → re-ingest (a failed ingest, or a gap)

**The limitation this does not remove, stated honestly.** A sweep is periodic, so
there is a window — up to one sweep interval — during which the tutor may cite
unpublished content. Running nightly *and* on the next publish for a course
shortens it. It cannot be closed without a platform event, and claiming otherwise
would be dishonest.

**Why the sweep is also the repair path.** It is not only a deletion tool: it
re-queues blocks that are live but unindexed, and retries entries in
`failed_ingestions`. A gap must be *detectable and recoverable*, never a silent
absence that surfaces later as "the tutor doesn't know that lesson" (Principle 8).
"""

from __future__ import annotations

import logging
import uuid

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True)
def reconcile_course(self, course_id: str, force: bool = False) -> dict:
    """Compare the published tree against the index and repair the difference.

    `force` lifts the blast-radius cap in `drift.compute_drift` and is reachable
    only from the management command — an unattended nightly job must not be
    able to empty an index off the back of one bad modulestore read.
    """
    from django.conf import settings
    from django.utils import timezone
    from opaque_keys.edx.keys import CourseKey

    from ..adapters import content_adapter
    from ..client import http
    from ..client.endpoints import send_leaves
    from ..drift import compute_drift
    from ..models import FailedIngestion

    course_key = CourseKey.from_string(course_id)
    offering_id = course_id
    trace_id = str(uuid.uuid4())

    # --- what the platform actually publishes ----------------------------
    # Read through content_adapter, which pins the published branch internally.
    # A sweep that accidentally read drafts would "repair" the index into
    # serving unpublished content — the exact failure it exists to prevent.
    live_leaves = list(content_adapter.iter_course_leaves(course_key))
    live_keys = {leaf.usage_key for leaf in live_leaves}

    # --- what we currently serve -----------------------------------------
    try:
        body = http.get(f"/coursemate/api/ingest/manifest/{offering_id}")
        indexed_keys = set(body.get("usage_keys") or [])
    except Exception:
        log.exception("sweep: could not read the index manifest for %s", offering_id)
        raise

    # The set arithmetic lives in `drift` so the decision that deletes content
    # is testable without Open edX — including its refusal to act on a course
    # read that returned nothing.
    drift = compute_drift(live_keys, indexed_keys, force=force)
    orphans, missing = drift.orphans, drift.missing
    if drift.refused:
        log.error("sweep REFUSED to prune %s: %s", offering_id, drift.refused)

    # --- remove what is no longer published -------------------------------
    if orphans:
        http.post(
            "/coursemate/api/ingest/prune",
            {"offering_id": offering_id, "usage_keys": orphans},
        )
        log.warning(
            "sweep: removed %d orphaned blocks from %s — unpublished or deleted "
            "with no event to tell us", len(orphans), offering_id,
        )

    # --- re-ingest what is published but absent ---------------------------
    # Deliberately NOT a swap: this tops up the active version rather than
    # replacing it, because a sweep that swapped would revert the course to
    # whatever version it read a moment ago — losing a reindex that landed in
    # between — and would briefly deactivate the whole course to fix a handful
    # of blocks.
    activated: list[str] = []
    if missing:
        version = _active_version(offering_id)
        if not version:
            # Never indexed, or the index was lost. Topping up an index that
            # does not exist would write chunks under a version nothing points
            # at. A full bootstrap is the right repair, and it is a deliberate
            # operator action, not something a nightly sweep should start.
            log.warning(
                "sweep: %s has %d unindexed blocks but no active version — "
                "run coursemate_bootstrap; the sweep will not bootstrap a course",
                offering_id, len(missing),
            )
        else:
            by_key = {leaf.usage_key: leaf for leaf in live_leaves}
            window = [by_key[k] for k in missing if k in by_key]

            # Clear any inactive rows left by a sweep that died between writing
            # and activating. Without this, the retry writes a second copy and
            # the block is cited twice.
            http.post(
                "/coursemate/api/ingest/prune",
                {"offering_id": offering_id, "usage_keys": missing},
            )

            meta = content_adapter.get_course_meta(course_key)
            for start in range(0, len(window), settings.COURSEMATE_INGEST_BATCH_SIZE):
                batch = window[start:start + settings.COURSEMATE_INGEST_BATCH_SIZE]
                send_leaves(
                    tenant=settings.COURSEMATE_TENANT,
                    course_id=course_id,
                    offering_id=offering_id,
                    course_version=meta.get("course_version"),
                    leaves=batch,
                    trace_id=trace_id,
                    run_id=version,   # live and die with the version being served
                    is_final=False,
                    topup=True,       # activate just these blocks; do not re-swap
                )
                activated.extend(leaf.usage_key for leaf in batch)
            log.warning("sweep: repaired %d missing blocks in %s", len(activated), offering_id)

    # --- close out recorded failures --------------------------------------
    # A block that failed every retry sits in this table until something proves
    # it landed. The sweep is that proof: it is the only thing that reads the
    # index and the course tree together.
    resolved = 0
    now_present = indexed_keys | set(activated)
    for row in FailedIngestion.objects.filter(course_id=course_id, resolved_at__isnull=True):
        if row.usage_key in now_present or row.usage_key not in live_keys:
            # Indexed now, or no longer published — either way it is not a gap.
            row.resolved_at = timezone.now()
            row.save(update_fields=["resolved_at"])
            resolved += 1

    unresolved = list(
        FailedIngestion.objects.filter(course_id=course_id, resolved_at__isnull=True)
        .values_list("usage_key", flat=True)[:50]
    )

    report = {
        "offering_id": offering_id,
        "live_blocks": len(live_keys),
        "indexed_blocks": len(indexed_keys),
        "orphans_removed": orphans,
        "refused": drift.refused,
        "missing_repaired": activated,
        "missing_unrepaired": sorted(set(missing) - set(activated)),
        "failures_resolved": resolved,
        "failures_outstanding": unresolved,
    }
    # Logged at WARNING when it found drift, because a sweep that silently fixes
    # things teaches nobody that content was being cited after unpublication.
    level = log.warning if (orphans or missing) else log.info
    level("sweep %s: live=%d indexed=%d orphans=%d missing=%d repaired=%d",
          offering_id, len(live_keys), len(indexed_keys), len(orphans),
          len(missing), len(activated))
    return report


@shared_task(bind=True)
def reconcile_all(self) -> dict:
    """Nightly sweep across every indexed course.

    Scheduled rather than event-driven because the thing it detects — an
    unpublish — produces no event to drive it.
    """
    from ..client import http

    # Ask the SERVICE what it serves. Sweeping every course on the instance would
    # treat each never-indexed course as "entirely missing" and pull it in — a
    # nightly job silently enabling the tutor for courses whose staff never asked
    # for it. But the obvious platform-side list, `CourseIndexState`, is written
    # only by the bootstrap task: `coursemate_reindex` indexes a course without
    # recording anything, so that list was empty on a stack serving 231 chunks
    # and the nightly sweep swept nothing while reporting success.
    course_ids = (http.get("/coursemate/api/ingest/offerings") or {}).get("offerings") or []
    for course_id in course_ids:
        reconcile_course.delay(course_id)
    log.info("nightly sweep queued %d indexed courses", len(course_ids))
    return {"courses_queued": len(course_ids)}


def _active_version(offering_id: str) -> str | None:
    """The version currently being served, so a top-up lands in it."""
    from ..client import http

    try:
        return (http.get(f"/coursemate/api/ingest/stats/{offering_id}") or {}).get(
            "active_version"
        )
    except Exception:  # noqa: BLE001
        log.exception("sweep: could not read active version for %s", offering_id)
        return None
