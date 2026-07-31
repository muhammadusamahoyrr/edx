"""Bootstrap indexing — design §5.1.

    ./manage.py cms coursemate_reindex --course <course_key>
    ./manage.py cms coursemate_reindex --all

**The bug this exists to prevent.** If ingestion were triggered only by
`XBLOCK_PUBLISHED`, installing CourseMate on a running Open edX and opening a
course published six months ago would give *"not covered in this course"* for
every question — no event ever fired for that content, and nobody re-publishes an
old course to wake up a plugin. The confidence guard would make that look like
correct behaviour, which is worse.

**No `--incremental` flag, deliberately.** The platform's own `reindex_studio`
dropped every flag it had and made incremental the default, because a mode that
re-does finished work is not one anybody should have to ask for.

`--inline` runs in this process instead of enqueuing. That is for operators and
for verification; the default path enqueues, because a command that embeds
hundreds of blocks in-process dies with the SSH session and resumes from nothing.
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Index published course content for the CourseMate tutor."

    def add_arguments(self, parser):
        parser.add_argument("--course", help="Course key, e.g. course-v1:Org+Num+Run")
        parser.add_argument("--all", action="store_true", help="Every course on the instance")
        parser.add_argument(
            "--inline", action="store_true",
            help="Run in this process rather than enqueuing (operators, verification)",
        )

    def handle(self, *args, **options):
        from opaque_keys.edx.keys import CourseKey

        if not options["course"] and not options["all"]:
            raise CommandError("Specify --course <key> or --all")

        from ...adapters import content_adapter

        if options["all"]:
            keys = content_adapter.list_course_keys()
        else:
            keys = [CourseKey.from_string(options["course"])]

        for key in keys:
            self.stdout.write(f"Indexing {key} ...")
            if options["inline"]:
                result = self._index_inline(key)
            else:
                from ...tasks.bootstrap import bootstrap_course
                bootstrap_course.delay(str(key))
                result = {"enqueued": True}
            self.stdout.write(self.style.SUCCESS(f"  {result}"))

    def _index_inline(self, course_key) -> dict:
        """Walk the published tree and push leaves to the service.

        Reads go through `content_adapter`, which owns the published-branch
        context internally — a caller cannot forget to pin it, which is the whole
        reason that API is shaped the way it is.
        """
        from django.conf import settings

        from ...adapters import content_adapter
        from ...client.endpoints import send_leaves

        leaves = list(content_adapter.iter_course_leaves(course_key))
        if not leaves:
            return {"blocks": 0, "note": "no supported leaves found"}

        meta = content_adapter.get_course_meta(course_key)
        trace_id = str(uuid.uuid4())
        batch = settings.COURSEMATE_INGEST_BATCH_SIZE

        written = chunks = 0
        failed: list[str] = []
        starts = list(range(0, len(leaves), batch))
        for start in starts:
            window = leaves[start : start + batch]
            # Every batch of this run shares run_id, and only the last one flips
            # the active pointer. Without that, each batch swapped itself in and
            # deactivated its predecessors — a 226-block course served 26 blocks
            # and reported success.
            accepted = send_leaves(
                run_id=trace_id,
                is_final=(start == starts[-1]),
                tenant=settings.COURSEMATE_TENANT,
                course_id=str(course_key),
                offering_id=str(course_key),
                course_version=meta.get("course_version"),
                leaves=window,
                trace_id=trace_id,
            )
            written += accepted.blocks_written
            chunks += accepted.chunks_written
            failed.extend(accepted.failed)
            self.stdout.write(
                f"    batch {start // batch + 1}: {accepted.blocks_written} blocks "
                f"-> {accepted.chunks_written} chunks, swapped={accepted.swapped}"
            )

        # Reconcile against the tree so a partial run is visible rather than
        # assumed complete (§5.1).
        return {
            "leaves_found": len(leaves),
            "blocks_written": written,
            "chunks_written": chunks,
            "failed": len(failed),
        }
