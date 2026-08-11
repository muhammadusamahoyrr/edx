"""The agent's memory layer, in the platform's own database.

Two tables, and the split between them is the point:

* `StudentMastery` holds counters per (student, offering, outcome).
* `MasteryAttempt` holds one row per recorded attempt, keyed by an idempotency
  digest, so a double-clicked Submit or a retried tool call counts once.

**Why here and not in Redis.** Measured on this deployment before the decision:
Redis runs `maxmemory-policy allkeys-lru`, `maxmemory 4 GB`, `appendonly yes`,
shared with Celery's broker. `allkeys-lru` evicts keys that have no TTL, AOF
persists the eviction, and nothing logs it — so durable learning state stored
there can silently vanish, and the only symptom would be recommendations getting
quietly worse.

**Why here and not in the reasoning service.** §3.1 rejected a service-side
conversation store because it would duplicate PII into a system platform
user-retirement does not reach. Mastery is the same kind of data and gets the same
answer: the platform owns it, the browser carries a snapshot, the service forgets
it after the turn.

The unique constraints are the load-bearing part. `(student_id, offering_id,
clo_id)` makes double-counting a database error rather than a race two workers can
both win, and `idempotency_key` does the same for replays.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("coursemate_platform", "0002_courseindexstate_run_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="MasteryAttempt",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("idempotency_key", models.CharField(max_length=64, unique=True)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="StudentMastery",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("student_id", models.CharField(db_index=True, max_length=64)),
                ("offering_id", models.CharField(db_index=True, max_length=255)),
                ("clo_id", models.CharField(max_length=64)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("correct", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("student_id", "offering_id", "clo_id")},
            },
        ),
    ]
