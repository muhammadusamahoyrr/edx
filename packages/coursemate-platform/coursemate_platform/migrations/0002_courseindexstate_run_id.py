"""Persist the in-flight run id on CourseIndexState.

Added because the enqueued bootstrap path was writing every chunk INACTIVE and
never activating any of them. Two defects, one symptom, both found by importing
a second course and noticing the index held 454 chunks across 2 versions with
only 227 active:

1. `bootstrap_course` called `send_leaves` without `run_id` or `is_final`, so
   the final batch never triggered verify-then-swap. The course kept serving the
   previous version and every run leaked a full copy of the course into the
   index — while the task returned `{'indexed': 222, 'total': 222}`, because
   that counts what the service ACCEPTED, not what became active.

2. `last_usage_key` was never cleared on success, so the next run resumed from
   the last leaf of the finished one, sliced the walk to nothing, sent no
   batches at all, and reported success.

Fixing (1) alone would have introduced a worse bug through the resume path: a
resumed run sends only the remaining tail, so swapping to a fresh version would
activate a fraction of the course and report a complete run. That is the
226-blocks-indexed-26-served failure again. Hence this column — a resumed run
continues the SAME version, and the pointer still flips exactly once.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("coursemate_platform", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseindexstate",
            name="run_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
