"""Platform-side state.

The first two tables exist to satisfy Principle 8 — *never fail silently*. A gap
in the index must be **detectable**, never a quiet absence that surfaces later as
"the tutor doesn't know that lesson."

The last two are the agent's memory layer, and they are here rather than in the
reasoning service for the reason §3.1 already settled for chat history: this is
per-student data, and a second copy in a system platform user-retirement does not
reach would make "the platform keeps each student's data" false.

**They are also not in Redis, and that was measured rather than assumed.** This
deployment's Redis runs `maxmemory-policy allkeys-lru` with a 4 GB cap and
`appendonly yes`, shared with Celery's broker. `allkeys-lru` evicts keys *that have
no TTL* under memory pressure; AOF then persists the eviction; nothing logs it.
Mastery is durable learning state with no TTL, so it would silently disappear —
and the symptom would be study recommendations quietly getting worse, which is the
invisible-failure shape this project keeps finding. Redis may cache mastery. It may
not own it.
"""

from __future__ import annotations

from django.db import models, transaction
from django.utils import timezone


class CourseIndexState(models.Model):
    """Resumable bootstrap progress for one course.

    Not a `last_indexed_at` display field. The platform's own `reindex_studio`
    tracks completion state and resumes if interrupted, and it does so because a
    process that indexes hundreds of blocks in one go *will* be interrupted —
    a closed terminal, a restarted worker, an OOM kill. Restarting from zero each
    time is how a large course never finishes indexing at all.
    """

    course_id = models.CharField(max_length=255, unique=True, db_index=True)

    block_count = models.PositiveIntegerField(default=0)
    blocks_indexed = models.PositiveIntegerField(default=0)
    #: Walk position, so a resumed run continues rather than restarts.
    #:
    #: **Cleared when a run completes.** Leaving it set meant the next bootstrap
    #: resumed from the LAST leaf of the finished run, sliced the walk down to
    #: nothing, sent no batches, and reported success — so re-indexing a course
    #: through the enqueued path silently did nothing.
    last_usage_key = models.CharField(max_length=255, blank=True, default="")
    #: The version every batch of the current run writes under, persisted so a
    #: RESUMED run continues that version instead of opening a new one.
    #:
    #: Without it, a resume writes only the remaining tail under a fresh version
    #: and then swaps to it — activating a fraction of the course while
    #: reporting a complete run. That is the 226-blocks-indexed-26-served bug
    #: exactly, arriving through the resume path instead of the batch path.
    #:
    #: Empty means no run is in flight.
    run_id = models.CharField(max_length=64, blank=True, default="")

    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        app_label = "coursemate_platform"

    @classmethod
    def for_course(cls, course_id: str) -> "CourseIndexState":
        state, _ = cls.objects.get_or_create(course_id=course_id)
        return state

    def last_indexed_display(self) -> str:
        if not self.last_indexed_at:
            return "never"
        return timezone.localtime(self.last_indexed_at).strftime("%Y-%m-%d %H:%M")

    @property
    def is_complete(self) -> bool:
        return self.block_count > 0 and self.blocks_indexed >= self.block_count


class FailedIngestion(models.Model):
    """A block that failed all retries.

    The reconciliation sweep reports on these (§5.2, §5.4). This table is the
    difference between a detectable gap and a silent one — without it, a block
    that failed to embed simply never appears, and the tutor reports "not covered
    in this course," which reads as correct behaviour.
    """

    course_id = models.CharField(max_length=255, db_index=True)
    usage_key = models.CharField(max_length=255, db_index=True)
    version = models.CharField(max_length=255, blank=True, default="")

    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=1)
    first_failed_at = models.DateTimeField(auto_now_add=True)
    last_failed_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "coursemate_platform"
        unique_together = ("course_id", "usage_key")

    @classmethod
    def record(cls, course_id: str, usage_key: str, version: str, error: str) -> None:
        row, created = cls.objects.get_or_create(
            course_id=course_id,
            usage_key=usage_key,
            defaults={"version": version, "error": error},
        )
        if not created:
            row.attempts += 1
            row.error = error
            row.version = version
            row.resolved_at = None
            row.save(update_fields=["attempts", "error", "version", "resolved_at", "last_failed_at"])


class MasteryAttempt(models.Model):
    """One recorded practice attempt, keyed for idempotency.

    A ledger row rather than a flag on `StudentMastery`, because the counter and
    the "have I already counted this?" question have different shapes: the counter
    is one row per outcome, and the question is one row per attempt.

    Needed twice over. A student double-clicks Submit. And the agent loop may
    retry a tool call — read-only today, but "the write is idempotent" is what
    makes that safe to keep saying if it ever is not.
    """

    #: sha256 over (offering_id, student_id, clo_id, question_id, attempt_id),
    #: computed by `coursemate_contracts.mastery.idempotency_key` so both sides
    #: derive it identically. Unique, so a replay is a database no-op rather than
    #: a check-then-write race two workers can both win.
    idempotency_key = models.CharField(max_length=64, unique=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "coursemate_platform"


class StudentMastery(models.Model):
    """Practice counters for one student against one learning outcome.

    **Counters, not a score.** Collapsing `(attempts, correct)` into a single
    mastery number is a calibration question — spaced repetition, decay,
    difficulty weighting — and choosing a formula now would bake an unvalidated
    one into stored data. Counters can be rescored later; a stored score cannot be
    un-rounded. The design pass deferred mastery-algorithm versioning explicitly,
    and storing the inputs rather than the output is what keeps that deferral
    cheap.

    Keyed on `offering_id`, never `course_id`: CS-101 Fall 2026 is a different
    cohort from the same course a year later, and carrying last year's mastery
    into this year's plan would be wrong in the direction that hides gaps.
    """

    #: The platform's numeric user id, matching the JWT's `sub`.
    student_id = models.CharField(max_length=64, db_index=True)
    offering_id = models.CharField(max_length=255, db_index=True)
    clo_id = models.CharField(max_length=64)
    #: `easy` | `medium` | `hard`, or `""` when the source question carried no
    #: difficulty estimate — which a freshly extracted pack usually does.
    #:
    #: Empty string rather than NULL because it is part of a unique constraint,
    #: and SQL treats every NULL as distinct: two unbanded attempts at the same
    #: outcome would create two rows and the idempotency guarantee would hold
    #: while the counter still double-counted.
    #:
    #: Per band because "struggling with CLO-3" is not one fact — a student can
    #: be solid on the easy items and lost on the hard ones, and one counter
    #: averages that into a number that recommends neither.
    difficulty_band = models.CharField(max_length=16, blank=True, default="")

    #: `self_reported` | `evaluated`, from `coursemate_contracts.MasterySource`.
    #:
    #: **Part of the unique constraint, for the reason `difficulty_band` is.** A
    #: single column that merely labelled the row would flip value as attempts of
    #: different provenance arrived, and the counter it labels would blend a
    #: self-report with a grade — presenting the first with the authority of the
    #: second. Separate rows keep them separable; `MasterySnapshot.by_clo()` sums
    #: them, so everything that wants the total still gets it.
    #:
    #: Not nullable, and defaulted rather than blank: unlike a band, which is
    #: genuinely unknown on an unscored question, provenance is always known at
    #: the moment of writing. There is no honest "unknown source".
    source = models.CharField(max_length=16, default="self_reported")

    attempts = models.PositiveIntegerField(default=0)
    correct = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "coursemate_platform"
        unique_together = (
            "student_id", "offering_id", "clo_id", "difficulty_band", "source",
        )

    @classmethod
    def snapshot(cls, student_id: str, offering_id: str) -> list[dict]:
        """What the XBlock seeds into the page, for the browser to carry (§3.1).

        Plain dicts rather than the pydantic model: `coursemate_contracts` is
        imported into LMS processes and stays pure (contract 6), and this file is
        Django. The contract type is reconstructed service-side from this shape.
        """
        return [
            {"clo_id": r.clo_id,
             # "" is how the DB spells "unbanded"; the contract spells it None.
             "difficulty_band": r.difficulty_band or None,
             # Carried so the browser can show a self-report as a self-report.
             # Always populated — the row cannot exist without a provenance.
             "source": r.source,
             "attempts": r.attempts, "correct": r.correct}
            for r in cls.objects.filter(
                student_id=student_id, offering_id=offering_id
            ).order_by("clo_id", "difficulty_band", "source")
        ]

    @classmethod
    def record(
        cls,
        *,
        idempotency_key: str,
        student_id: str,
        offering_id: str,
        clo_id: str,
        correct: bool,
        difficulty_band: str | None = None,
        source: str = "self_reported",
    ) -> dict:
        """Count one attempt, exactly once.

        The ledger insert is what makes this idempotent, and it is attempted
        FIRST. Incrementing the counter and then recording the key would leave a
        window where a crash double-counts; doing it in this order means a crash
        loses the attempt instead — the safe direction, since a missed attempt
        makes the plan slightly stale while a double-counted one makes the
        mastery number wrong forever.

        Both statements are in one transaction, so a partial apply is not
        reachable at all. `atomic` plus a UNIQUE constraint, rather than
        get-or-create-then-check: two workers racing a double-click both pass a
        check, and only one survives a constraint.
        """
        with transaction.atomic():
            _, created = MasteryAttempt.objects.get_or_create(
                idempotency_key=idempotency_key
            )
            if not created:
                # Already counted. Return the CURRENT state rather than nothing:
                # the caller is a retry and still needs an answer, and returning
                # a different shape on the second call is how a retry path grows
                # its own bugs.
                # Filtered by source too, now that it is part of the unique key:
                # without it this could read a DIFFERENT row than the one the
                # original write touched and report someone else's totals back
                # to a retry.
                row = cls.objects.filter(
                    student_id=student_id, offering_id=offering_id, clo_id=clo_id,
                    difficulty_band=difficulty_band or "", source=source,
                ).first()
                return {
                    "applied": False,
                    "attempts": row.attempts if row else 0,
                    "correct": row.correct if row else 0,
                    "source": source,
                }

            row, _ = cls.objects.get_or_create(
                student_id=student_id, offering_id=offering_id, clo_id=clo_id,
                difficulty_band=difficulty_band or "", source=source,
            )
            row.attempts += 1
            if correct:
                row.correct += 1
            row.save(update_fields=["attempts", "correct", "updated_at"])
            return {"applied": True, "attempts": row.attempts,
                    "correct": row.correct, "source": source}
