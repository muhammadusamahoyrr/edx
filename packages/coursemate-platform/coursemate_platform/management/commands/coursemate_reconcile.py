"""Reconciliation sweep — design §5.4.

    ./manage.py cms coursemate_reconcile --course course-v1:Org+Num+Run
    ./manage.py cms coursemate_reconcile --all

Run nightly. This is the ONLY mitigation for unpublished content: `openedx-events`
has no unpublish event, so nothing tells us when an instructor unpublishes a unit,
and without this sweep the tutor keeps citing content students cannot see.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Compare the published tree against the index; remove orphans, re-ingest gaps."

    def add_arguments(self, parser):
        parser.add_argument("--course", help="Course key")
        parser.add_argument("--all", action="store_true", help="Every course")
        parser.add_argument(
            "--force", action="store_true",
            help="Lift the blast-radius cap after checking the course by hand",
        )
        parser.add_argument(
            "--inline", action="store_true",
            help="Run here rather than enqueuing (operators, verification)",
        )

    def handle(self, *args, **options):
        from opaque_keys.edx.keys import CourseKey

        from ...client import http
        from ...tasks.reconcile import reconcile_course

        if not options["course"] and not options["all"]:
            raise CommandError("Specify --course <key> or --all")

        # --all means every course the SERVICE serves, not every course on the
        # instance: sweeping a never-indexed course would read as "entirely
        # missing" and pull it in. Opting in is a bootstrap, never a side effect.
        keys = (
            (http.get("/coursemate/api/ingest/offerings") or {}).get("offerings") or []
            if options["all"]
            else [str(CourseKey.from_string(options["course"]))]
        )

        for key in keys:
            if options["inline"]:
                report = reconcile_course(str(key), force=options["force"])
                drift = (report["orphans_removed"] or report["missing_repaired"]
                         or report["missing_unrepaired"])
                style = self.style.WARNING if drift else self.style.SUCCESS
                self.stdout.write(style(
                    f"  {key}: live={report['live_blocks']} "
                    f"indexed={report['indexed_blocks']} "
                    f"orphans={len(report['orphans_removed'])} "
                    f"repaired={len(report['missing_repaired'])} "
                    f"unrepaired={len(report['missing_unrepaired'])}"
                ))
                if report["refused"]:
                    self.stdout.write(self.style.ERROR(f"      REFUSED: {report['refused']}"))
                for orphan in report["orphans_removed"][:10]:
                    self.stdout.write(f"      removed (no longer published): {orphan}")
                if report["failures_outstanding"]:
                    self.stdout.write(self.style.ERROR(
                        f"      {len(report['failures_outstanding'])} block(s) still "
                        f"failing ingest — these are gaps the tutor cannot answer from"
                    ))
            else:
                reconcile_course.delay(str(key), force=options["force"])
                self.stdout.write(f"  {key}: enqueued")
