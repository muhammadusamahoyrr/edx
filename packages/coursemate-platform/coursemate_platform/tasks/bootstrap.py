"""Bootstrap — design §5.1.

**The bug this exists to prevent.** If ingestion were triggered only by
XBLOCK_PUBLISHED, then installing CourseMate on a running Open edX and opening a
course published six months ago would give "not covered in this course" for every
question — no event ever fired for that content, and nobody re-publishes an old
course to wake up a plugin. The confidence guard would make that look like correct
behaviour, which is worse. It is the most likely way a demo fails.

**Resumable, because the platform learned this before us.** `reindex_studio`
enqueues a task that tracks completion state and resumes if interrupted, and it
dropped its `--incremental` flag entirely because incremental is the only sane
default. A command that embeds hundreds of blocks in-process dies with the
session and restarts from zero.
"""

from __future__ import annotations

import logging
import uuid

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True)
def bootstrap_course(self, course_id: str):
    from django.conf import settings
    from django.utils import timezone
    from opaque_keys.edx.keys import CourseKey

    from ..adapters import content_adapter
    from ..client.endpoints import send_leaves
    from ..locks import bootstrap_lock
    from ..models import CourseIndexState, FailedIngestion

    key = CourseKey.from_string(course_id)
    state = CourseIndexState.for_course(course_id)
    trace_id = str(uuid.uuid4())

    try:
        leaves = list(content_adapter.iter_course_leaves(key))
        state.block_count = len(leaves)

        # **One run, one version.** A resumed run continues the version the
        # interrupted one opened, because swapping to a version holding only the
        # resumed tail would activate a fraction of the course while reporting a
        # complete run — the 226-indexed-26-served failure, arriving through the
        # resume path instead of the batch path.
        run_id = state.run_id or trace_id
        state.run_id = run_id
        state.save(update_fields=["block_count", "run_id"])

        # Resume: skip everything already done in a previous interrupted run.
        if state.last_usage_key:
            keys = [leaf.usage_key for leaf in leaves]
            if state.last_usage_key in keys:
                leaves = leaves[keys.index(state.last_usage_key) + 1 :]
                log.info("coursemate: resuming %s after %s", course_id, state.last_usage_key)

        meta = content_adapter.get_course_meta(key)
        batch = settings.COURSEMATE_INGEST_BATCH_SIZE
        starts = list(range(0, len(leaves), batch))

        for start in starts:
            window = leaves[start : start + batch]
            result = send_leaves(
                # Without these two the service never verifies and never swaps:
                # every chunk is written INACTIVE, the course keeps serving the
                # previous version, and each run leaks a whole copy of the
                # course into the index. The task still returned indexed==total,
                # because that counts what the service ACCEPTED rather than what
                # became active.
                run_id=run_id,
                is_final=(start == starts[-1]),
                tenant=settings.COURSEMATE_TENANT,
                course_id=course_id,
                offering_id=course_id,
                course_version=meta.get("course_version"),
                leaves=window,
                trace_id=trace_id,
            )
            for failed in result.failed:
                FailedIngestion.record(course_id, failed, "", "service rejected")

            state.blocks_indexed += result.blocks_written
            state.last_usage_key = window[-1].usage_key
            state.save(update_fields=["blocks_indexed", "last_usage_key"])

        if not starts:
            # Everything was already sent by an attempt that never finalised, so
            # there is nothing left to write but a version sitting inactive.
            # A final empty batch makes the service verify and swap it in;
            # without this the course serves the old version forever and every
            # later run resumes past the end and does nothing.
            log.info("coursemate: %s already fully sent; finalising run %s", course_id, run_id)
            send_leaves(
                run_id=run_id,
                is_final=True,
                tenant=settings.COURSEMATE_TENANT,
                course_id=course_id,
                offering_id=course_id,
                course_version=meta.get("course_version"),
                leaves=[],
                trace_id=trace_id,
            )

        state.last_indexed_at = timezone.now()
        state.last_error = ""
        # Clear the walk position AND the run. Leaving them set made the next
        # run resume from the last leaf of this one, slice the walk to nothing,
        # send no batches, and report success.
        state.last_usage_key = ""
        state.run_id = ""
        state.save(update_fields=[
            "last_indexed_at", "last_error", "last_usage_key", "run_id",
        ])
        bootstrap_lock.release(course_id)

        # Reconcile the final count against the course tree, so a partial run is
        # visible rather than assumed complete (§5.1).
        log.info(
            "coursemate: bootstrap %s -> %s/%s blocks",
            course_id, state.blocks_indexed, state.block_count,
        )
        return {"indexed": state.blocks_indexed, "total": state.block_count}

    except Exception as exc:  # noqa: BLE001
        state.last_error = str(exc)
        state.save(update_fields=["last_error"])
        # Release the lock but start a cooldown, so a persistently failing course
        # cannot be hammered by a page refresh (§5.1).
        bootstrap_lock.mark_failed(course_id)
        raise
