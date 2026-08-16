"""The XBlock's exam-prep surface: the mastery courier and the one write.

Both halves of §3.1's courier pattern are checked here — what the block seeds into
the page, and what it accepts back — because the property that matters is that the
service holds no per-student state, and that only holds if the platform really is
carrying it.

The block is exercised through unbound methods on a stand-in rather than a real
XBlock instance. Instantiating one needs an XBlock runtime, which needs Open edX
— and the logic under test is the mastery read, the trimming and the identity
handling, none of which the runtime contributes to.
"""

from __future__ import annotations

import types

import pytest
from coursemate_contracts.mastery import MasterySnapshot
from coursemate_platform.xblock.tutor_block import (
    MASTERY_WINDOW_CLOS,
    CourseMateTutorXBlock,
)

OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"


class _Stub:
    """Enough of the block for the methods under test."""

    def __init__(self, user_id="42"):
        self._user_id = user_id

    def _user(self):
        if self._user_id is None:
            return None
        return types.SimpleNamespace(
            opt_attrs={"edx-platform.user_id": self._user_id, "edx-platform.username": "alice"}
        )

    def _offering_id(self):
        return OFFERING

    _course_id = _offering_id


def _snapshot(stub):
    return CourseMateTutorXBlock._mastery_snapshot(stub)


def _record(stub, **payload):
    # `json_handler` wraps the method; the underlying function is what we call.
    fn = CourseMateTutorXBlock.record_attempt.__wrapped__
    return fn(stub, payload)


# --- the read side: seeding the courier ------------------------------------


def test_an_anonymous_view_still_renders(monkeypatch):
    """No user means no mastery — never an exception. A mastery lookup must not
    be able to break the lesson page."""
    assert _snapshot(_Stub(user_id=None)) == {
        "offering_id": OFFERING, "clos": [], "truncated": False
    }


def test_a_failed_lookup_degrades_the_plan_rather_than_the_page(monkeypatch):
    """The honest fallback: every outcome is treated as unattempted, the plan's
    ordering gets worse, and the student still sees their lesson."""
    import coursemate_platform.models as models

    def explode(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(models.StudentMastery, "snapshot", staticmethod(explode))
    assert _snapshot(_Stub())["clos"] == []


def test_the_snapshot_is_trimmed_and_says_so(monkeypatch):
    """Told, not silently dropped. 'No history for that outcome' and 'trimmed
    away' are different facts, and the agent is given the difference."""
    import coursemate_platform.models as models

    rows = [{"clo_id": f"CLO-{i}", "attempts": 1, "correct": 1} for i in range(200)]
    monkeypatch.setattr(models.StudentMastery, "snapshot", staticmethod(lambda *a: rows))

    snapshot = _snapshot(_Stub())
    assert len(snapshot["clos"]) == MASTERY_WINDOW_CLOS
    assert snapshot["truncated"] is True


def test_the_trim_keeps_the_payload_contract_valid(monkeypatch):
    """`MasterySnapshot` caps `clos` at 64. If the block trimmed to anything
    larger, a student with many outcomes would get a 422 from their own tutor —
    so the two constants have to agree, and this is what notices when they stop."""
    import coursemate_platform.models as models

    rows = [{"clo_id": f"CLO-{i}", "attempts": 1, "correct": 1} for i in range(500)]
    monkeypatch.setattr(models.StudentMastery, "snapshot", staticmethod(lambda *a: rows))

    MasterySnapshot(**_snapshot(_Stub()))  # raises if the cap disagrees


def test_the_snapshot_is_scoped_to_this_offering(monkeypatch):
    seen = {}
    import coursemate_platform.models as models

    def spy(student_id, offering_id):
        seen.update(student_id=student_id, offering_id=offering_id)
        return []

    monkeypatch.setattr(models.StudentMastery, "snapshot", staticmethod(spy))
    _snapshot(_Stub())
    assert seen == {"student_id": "42", "offering_id": OFFERING}


# --- the write side: the only write in Feature B ---------------------------


def test_an_anonymous_attempt_is_refused():
    assert _record(_Stub(user_id=None), clo_id="C", question_id="Q",
                   attempt_id="a", correct=True) == {"error": "unauthenticated"}


@pytest.mark.parametrize("missing", ["clo_id", "question_id", "attempt_id"])
def test_every_key_component_is_required(missing):
    """`attempt_id` especially. Defaulting it would make every attempt at the
    same question share a key, so a student's second try at a question they got
    wrong would be discarded as a replay and their record would freeze at the
    first answer they ever gave."""
    payload = {"clo_id": "C", "question_id": "Q", "attempt_id": "a", "correct": True}
    payload[missing] = ""
    assert "error" in _record(_Stub(), **payload)


def test_the_student_id_comes_from_the_session_not_the_payload(django_db):
    """The browser carries mastery OUT. It does not get to say whose it is on the
    way back."""
    from coursemate_platform.models import StudentMastery

    _record(_Stub(user_id="42"), clo_id="CLO-1", question_id="Q1",
            attempt_id="a1", correct=True, student_id="99", offering_id="course-v1:Evil+X+Y")

    assert StudentMastery.snapshot("99", OFFERING) == []
    assert StudentMastery.snapshot("42", OFFERING) == [
        {"clo_id": "CLO-1", "difficulty_band": None, "source": "self_reported",
         "attempts": 1, "correct": 1}
    ]


def test_a_double_clicked_submit_counts_once(django_db):
    """End to end through the handler, not just the model: the handler is what
    derives the key, and a handler that derived a fresh one each call would make
    the model's idempotency irrelevant."""
    args = dict(clo_id="CLO-1", question_id="Q1", attempt_id="a1", correct=True)
    first = _record(_Stub(), **args)
    second = _record(_Stub(), **args)

    assert first == {"applied": True, "attempts": 1, "correct": 1,
                     "source": "self_reported"}
    assert second == {"applied": False, "attempts": 1, "correct": 1,
                      "source": "self_reported"}


def test_a_separator_in_an_identifier_is_refused(django_db):
    """Refused rather than sanitised — silently rewriting an id makes a collision
    harder to find, not less likely."""
    assert _record(_Stub(), clo_id="CLO-1", question_id="Q\x1f1",
                   attempt_id="a1", correct=True) == {"error": "invalid identifier"}


def test_the_tab_is_off_by_default():
    """An instructor who wants the tutor in every unit does not necessarily want
    a revision planner in every unit."""
    assert CourseMateTutorXBlock.exam_prep_enabled.default is False


# --- C2: the handler will not accept a claim it cannot back ----------------


def test_an_attempt_records_as_self_reported_by_default(django_db):
    out = _record(_Stub(), clo_id="CLO-1", question_id="Q1", attempt_id="a1", correct=True)
    assert out.get("source") == "self_reported", out


def test_a_payload_claiming_evaluation_is_refused(django_db):
    """Nothing in this deployment can evaluate an answer — there is no answer
    key. Accepting the word because the column can hold it would put an unearned
    claim into durable student data, which is the confusion `source` exists to
    end."""
    out = _record(_Stub(), clo_id="CLO-1", question_id="Q1", attempt_id="a1",
                  correct=True, source="evaluated")
    assert "error" in out, out
    assert "nothing here evaluates answers" in out["error"]


def test_a_refused_source_writes_nothing(django_db):
    from coursemate_platform.models import StudentMastery

    _record(_Stub(), clo_id="CLO-1", question_id="Q1", attempt_id="a1",
            correct=True, source="evaluated")
    assert StudentMastery.snapshot("7", OFFERING) == []


def test_an_explicit_self_reported_source_is_accepted(django_db):
    out = _record(_Stub(), clo_id="CLO-1", question_id="Q1", attempt_id="a1",
                  correct=True, source="self_reported")
    assert out.get("applied") is True, out
