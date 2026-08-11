"""The memory layer's one hard guarantee: an attempt counts exactly once.

Everything downstream of mastery — which outcome a plan leads with, which
questions a student is shown — is a ranking over these counters. A double count
is not a visible failure; it is a plan that is quietly slightly wrong, forever,
which is the shape of bug this project keeps finding.
"""

from __future__ import annotations

import pytest
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot, idempotency_key

OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"


def key(**kw) -> str:
    base = dict(offering_id=OFFERING, student_id="42", clo_id="CLO-1",
                question_id="Q1", attempt_id="a1")
    return idempotency_key(**{**base, **kw})


# --- the key itself (pure, no database) -----------------------------------


def test_the_same_attempt_yields_the_same_key():
    assert key() == key()


@pytest.mark.parametrize(
    "field", ["offering_id", "student_id", "clo_id", "question_id", "attempt_id"]
)
def test_every_component_changes_the_key(field):
    """A component that does not affect the digest is a component two different
    attempts can collide on."""
    assert key() != key(**{field: "different"})


def test_components_cannot_be_shifted_across_the_boundary():
    """The classic concatenation bug: ("a|b", "c") and ("a", "b|c") hashing the
    same. Here it would merge two students' attempts into one counter."""
    assert key(student_id="42", clo_id="CLO-1") != key(student_id="42CLO-1", clo_id="")


def test_a_component_containing_the_separator_is_refused():
    """Refused rather than sanitised. Silently rewriting an id makes a collision
    harder to find, not less likely to happen."""
    with pytest.raises(ValueError, match="separator"):
        key(question_id="Q\x1f1")


def test_a_second_attempt_at_the_same_question_is_a_different_key():
    """`attempt_id` is why. Without it a student's second try at a question they
    got wrong would be discarded as a replay, and their record would freeze at
    the first answer they ever gave."""
    assert key(attempt_id="a1") != key(attempt_id="a2")


# --- the write path (Django) ----------------------------------------------


def test_a_replayed_attempt_is_counted_once(django_db):
    """A double-clicked Submit, or a retried tool call. Both must count once."""
    from coursemate_platform.models import StudentMastery

    k = key()
    first = StudentMastery.record(
        idempotency_key=k, student_id="42", offering_id=OFFERING,
        clo_id="CLO-1", correct=True,
    )
    second = StudentMastery.record(
        idempotency_key=k, student_id="42", offering_id=OFFERING,
        clo_id="CLO-1", correct=True,
    )

    assert first == {"applied": True, "attempts": 1, "correct": 1}
    # The replay still gets an answer, and the SAME shape — a retry path that
    # returns something different on the second call grows its own bugs.
    assert second == {"applied": False, "attempts": 1, "correct": 1}


def test_distinct_attempts_accumulate(django_db):
    from coursemate_platform.models import StudentMastery

    for i, correct in enumerate([True, False, True], start=1):
        StudentMastery.record(
            idempotency_key=key(attempt_id=f"a{i}"), student_id="42",
            offering_id=OFFERING, clo_id="CLO-1", correct=correct,
        )
    row = StudentMastery.objects.get(student_id="42", offering_id=OFFERING, clo_id="CLO-1")
    assert (row.attempts, row.correct) == (3, 2)


def test_an_incorrect_attempt_counts_as_an_attempt(django_db):
    """Attempts and correct move independently. Counting only correct answers
    would make a student who has failed six times look identical to one who has
    never tried — and those want opposite recommendations."""
    from coursemate_platform.models import StudentMastery

    StudentMastery.record(
        idempotency_key=key(), student_id="42", offering_id=OFFERING,
        clo_id="CLO-1", correct=False,
    )
    row = StudentMastery.objects.get(student_id="42", offering_id=OFFERING, clo_id="CLO-1")
    assert (row.attempts, row.correct) == (1, 0)


def test_mastery_does_not_leak_across_offerings(django_db):
    """CS-101 Fall 2026 is a different cohort from the same course a year later.
    Carrying last year's mastery into this year's plan would hide gaps."""
    from coursemate_platform.models import StudentMastery

    StudentMastery.record(
        idempotency_key=key(), student_id="42", offering_id=OFFERING,
        clo_id="CLO-1", correct=True,
    )
    assert StudentMastery.snapshot("42", OTHER) == []


def test_mastery_does_not_leak_across_students(django_db):
    from coursemate_platform.models import StudentMastery

    StudentMastery.record(
        idempotency_key=key(), student_id="42", offering_id=OFFERING,
        clo_id="CLO-1", correct=True,
    )
    assert StudentMastery.snapshot("99", OFFERING) == []


def test_a_new_student_reads_empty_not_missing(django_db):
    """Empty is a valid answer meaning 'no history'. The agent is told this
    explicitly so it never reads it as a broken lookup."""
    from coursemate_platform.models import StudentMastery

    assert StudentMastery.snapshot("new-student", OFFERING) == []


def test_the_snapshot_shape_validates_against_the_contract(django_db):
    """The XBlock emits plain dicts — `coursemate_contracts` stays pure and this
    file is Django — so the shape has to be checked against the model the service
    will rebuild it into, or the two drift with nothing failing."""
    from coursemate_platform.models import StudentMastery

    for i in (1, 2):
        StudentMastery.record(
            idempotency_key=key(clo_id=f"CLO-{i}"), student_id="42",
            offering_id=OFFERING, clo_id=f"CLO-{i}", correct=i == 1,
        )
    snapshot = MasterySnapshot(
        offering_id=OFFERING, clos=StudentMastery.snapshot("42", OFFERING)
    )
    assert [c.clo_id for c in snapshot.clos] == ["CLO-1", "CLO-2"]
    assert snapshot.by_clo()["CLO-1"].accuracy == 1.0
    assert snapshot.by_clo()["CLO-2"].accuracy == 0.0


def test_an_untried_outcome_reports_unknown_not_zero():
    """Pure contract check. 0.0 would rank an unattempted outcome level with one
    the student has failed repeatedly."""
    assert CLOMastery(clo_id="CLO-1").accuracy is None
    assert CLOMastery(clo_id="CLO-1", attempts=2, correct=0).accuracy == 0.0


# --- difficulty bands (migration 0004) -------------------------------------


def test_the_same_outcome_is_counted_separately_per_band(django_db):
    """"Struggling with CLO-3" is not one fact. A student solid on the easy items
    and lost on the hard ones must not be averaged into a single number that
    recommends neither."""
    from coursemate_platform.models import StudentMastery

    for i, (band, correct) in enumerate(
        [("easy", True), ("easy", True), ("hard", False), ("hard", False)], start=1
    ):
        StudentMastery.record(
            idempotency_key=key(attempt_id=f"a{i}"), student_id="42",
            offering_id=OFFERING, clo_id="CLO-1", difficulty_band=band, correct=correct,
        )

    rows = {r["difficulty_band"]: r for r in StudentMastery.snapshot("42", OFFERING)}
    assert rows["easy"]["correct"] == 2
    assert rows["hard"]["correct"] == 0
    assert rows["easy"]["attempts"] == rows["hard"]["attempts"] == 2


def test_an_unbanded_attempt_uses_one_row_not_many(django_db):
    """`""` rather than NULL, because the column is in a UNIQUE constraint and
    SQL treats every NULL as distinct. With NULL, two unbanded attempts would
    make two rows — the ledger would say "counted once" while the counter
    double-counted."""
    from coursemate_platform.models import StudentMastery

    for i in (1, 2):
        StudentMastery.record(
            idempotency_key=key(attempt_id=f"a{i}"), student_id="42",
            offering_id=OFFERING, clo_id="CLO-1", difficulty_band=None, correct=True,
        )

    rows = StudentMastery.snapshot("42", OFFERING)
    assert len(rows) == 1
    assert rows[0] == {"clo_id": "CLO-1", "difficulty_band": None,
                       "attempts": 2, "correct": 2}


def test_a_replay_is_still_counted_once_within_a_band(django_db):
    from coursemate_platform.models import StudentMastery

    k = key()
    a = StudentMastery.record(idempotency_key=k, student_id="42", offering_id=OFFERING,
                              clo_id="CLO-1", difficulty_band="hard", correct=True)
    b = StudentMastery.record(idempotency_key=k, student_id="42", offering_id=OFFERING,
                              clo_id="CLO-1", difficulty_band="hard", correct=True)
    assert (a["applied"], b["applied"]) == (True, False)
    assert b["attempts"] == 1


def test_the_snapshot_round_trips_bands_through_the_contract(django_db):
    """The XBlock emits plain dicts; the service rebuilds `MasterySnapshot`. If
    the shapes drift, the tab 422s against its own tutor."""
    from coursemate_platform.models import StudentMastery

    for i, band in enumerate(("easy", "medium", "hard"), start=1):
        StudentMastery.record(
            idempotency_key=key(attempt_id=f"a{i}"), student_id="42",
            offering_id=OFFERING, clo_id="CLO-1", difficulty_band=band, correct=i == 1,
        )

    snap = MasterySnapshot(offering_id=OFFERING,
                           clos=StudentMastery.snapshot("42", OFFERING))
    assert sorted(b for _, b in snap.by_clo_band()) == ["easy", "hard", "medium"]
    # Aggregated across bands for callers that rank whole outcomes.
    assert snap.by_clo()["CLO-1"].attempts == 3
    assert snap.by_clo()["CLO-1"].correct == 1
