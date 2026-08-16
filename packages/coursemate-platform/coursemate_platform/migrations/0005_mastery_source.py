"""Record HOW each mastery attempt was judged.

Every counter in this table was produced by a student pressing "I got this".
There is no answer key anywhere in the system, so nothing has verified any of it.
That is defensible — mastery ranks the student's own revision and reaches nothing
else — but it is only defensible while it is *legible*, and until now the row
carried no trace of where its number came from.

**This lands before evaluation exists, on purpose.** The moment an evaluated
attempt can be written, every row recorded up to that point becomes permanently
ambiguous: nothing could reconstruct afterwards whether a given 2026 attempt was
marked by a student or by a model. Provenance is cheap to record now and
impossible to recover later, so the column ships ahead of the thing that will use
its second value.

`source` joins the UNIQUE constraint rather than merely labelling the row, for
the reason `difficulty_band` did in 0004. A bare column would flip value as
attempts of different provenance arrived, and the counter beside it would blend a
self-report with a grade — presenting the first with the authority of the second.
Separate rows keep them separable, and `MasterySnapshot.by_clo()` sums across
them, so every existing reader still sees the same totals.

**What actually backfills the existing rows is `AddField`'s default**, applied
in the same DDL statement that adds the column. Saying so plainly matters: an
earlier draft of this file carried a `RunPython` that filtered on `source=""`
and described itself as the backfill. It was a no-op — the default had already
filled every row — and a migration whose comment claims a step it does not
perform is worse than one with no comment.

The sweep below is kept as a **safety net, described as one**: it catches a row
that reached the table without a source, which the default makes unlikely rather
than impossible (a concurrent writer against a partially-migrated schema, or a
re-run after a partial failure). It is idempotent and normally updates nothing.

Existing rows really were self-reported. That is a statement of fact about them,
not a convenient assumption — every one was written by the "I got this" handler,
which is the only writer that has ever existed.
"""

from django.db import migrations, models

SELF_REPORTED = "self_reported"


def sweep_missing_sources(apps, schema_editor):
    """Safety net. Normally updates 0 rows — `AddField`'s default got there first.

    Covers empty string and NULL both, since a row arriving by some path other
    than the default could carry either.
    """
    StudentMastery = apps.get_model("coursemate_platform", "StudentMastery")
    StudentMastery.objects.filter(source="").update(source=SELF_REPORTED)
    StudentMastery.objects.filter(source__isnull=True).update(source=SELF_REPORTED)


def unsweep(apps, schema_editor):
    """Reverse drops the column, so there is nothing to undo per row."""


class Migration(migrations.Migration):

    dependencies = [
        ("coursemate_platform", "0004_mastery_difficulty_band"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentmastery",
            name="source",
            field=models.CharField(default=SELF_REPORTED, max_length=16),
        ),
        migrations.RunPython(sweep_missing_sources, unsweep),
        # Widened, not replaced: existing rows keep their counts and simply
        # become the `self_reported` row for their outcome and band.
        migrations.AlterUniqueTogether(
            name="studentmastery",
            unique_together={
                ("student_id", "offering_id", "clo_id", "difficulty_band", "source"),
            },
        ),
    ]
