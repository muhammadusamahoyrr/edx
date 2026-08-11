"""Key mastery on the difficulty band as well as the outcome.

"Struggling with CLO-3" is not one fact. A student can be solid on the easy items
and lost on the hard ones, and a single counter averages those into a number that
recommends neither — which is exactly the recommendation mastery exists to make.
The practice generator selects a source question by band, so the record of how it
went has to be keyed the same way or the two disagree.

`difficulty_band` is `""` rather than NULL when the source question carries no
difficulty estimate, which a freshly extracted pack usually does. That is not
cosmetic: the column is part of a UNIQUE constraint, and SQL treats every NULL as
distinct, so two unbanded attempts at the same outcome would create two rows. The
idempotency ledger would still say "counted once" while the counter double-counted
— a partial state reporting success, which is this project's recurring bug.

The unique constraint is widened rather than replaced, so existing rows keep their
counts and simply become the `""` band.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("coursemate_platform", "0003_mastery"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentmastery",
            name="difficulty_band",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AlterUniqueTogether(
            name="studentmastery",
            unique_together={("student_id", "offering_id", "clo_id", "difficulty_band")},
        ),
    ]
