"""What `persist_turn` writes back, and what survives a reload.

Chat history lives in `Scope.user_state` and is re-rendered on every page load,
so anything missing from the stored turn is silently absent for the rest of that
student's life in the course.

`unsupported` was missing until 2026-08-12. The live answer carried "this
sentence is not supported by the material"; the reloaded one carried the
sentence and no warning, which is the one direction the failure must not go —
a lost citation weakens an answer's support, a lost warning invents it.
"""

from __future__ import annotations

import pytest
from coursemate_platform.xblock.citations import MAX_UNSUPPORTED_PER_TURN
from coursemate_platform.xblock.tutor_block import (
    HISTORY_WINDOW_TURNS,
    CourseMateTutorXBlock,
)


class _Stub:
    """Enough of the block for `persist_turn`."""

    def __init__(self, history=None):
        self.history = list(history or [])


def _persist(stub, **payload):
    fn = CourseMateTutorXBlock.persist_turn.__wrapped__
    return fn(stub, payload)


def _tutor_turn(stub):
    return stub.history[-1]


# --- the marks are stored ---------------------------------------------------


def test_marks_are_stored_with_the_turn():
    stub = _Stub()
    _persist(
        stub,
        question="What is a cohort?",
        answer="A cohort is a group. Deadlock cannot occur here.",
        citations=[],
        unsupported=["Deadlock cannot occur here."],
    )
    assert _tutor_turn(stub)["unsupported"] == ["Deadlock cannot occur here."]


def test_an_answer_with_no_marks_stores_an_empty_list():
    """Empty and absent must be the same shape on the way out, or the renderer
    has two cases to get right instead of one."""
    stub = _Stub()
    _persist(stub, question="q", answer="a", citations=[], unsupported=[])
    assert _tutor_turn(stub)["unsupported"] == []


def test_a_payload_with_no_unsupported_key_still_saves():
    """Backward compatibility on the WRITE side: an older cached page, or any
    client that has not been updated, omits the field entirely. That must persist
    the turn rather than lose it."""
    stub = _Stub()
    out = _persist(stub, question="q", answer="a", citations=[])
    assert out["saved"] is True
    assert _tutor_turn(stub)["unsupported"] == []


def test_citations_are_still_stored_alongside():
    """Regression: the marks were added next to citations, not instead of them."""
    stub = _Stub()
    _persist(
        stub, question="q", answer="a",
        citations=[{"usage_key": "block-v1:x", "display_name": "Cohorts", "url": "/j/x"}],
        unsupported=["doubtful"],
    )
    turn = _tutor_turn(stub)
    assert turn["citations"][0]["display_name"] == "Cohorts"
    assert turn["unsupported"] == ["doubtful"]


# --- the browser's own payload ----------------------------------------------


def test_the_browser_shaped_payload_round_trips():
    """The body `tutor.js` actually sends after a verified answer: question,
    answer, citations, unsupported — in that shape, with the marks as plain
    strings taken from `frame.text`."""
    stub = _Stub()
    _persist(
        stub,
        question="What is a content group?",
        answer="A content group serves different experiences. It also cures scurvy.",
        citations=[
            {"usage_key": "block-v1:OpenedX+DemoX+DemoCourse+type@html+block@cg",
             "display_name": "Content Groups",
             "url": "/courses/course-v1:OpenedX+DemoX+DemoCourse/jump_to/block-v1:cg"},
        ],
        unsupported=["It also cures scurvy."],
    )
    student, tutor = stub.history
    assert student == {"role": "student", "content": "What is a content group?"}
    assert tutor["role"] == "tutor"
    assert tutor["unsupported"] == ["It also cures scurvy."]
    assert len(tutor["citations"]) == 1


# --- untrusted input --------------------------------------------------------


def test_marks_are_sanitised_not_stored_raw():
    """`persist_turn` is called by the student's own page, so the list is
    untrusted even though the marks originated with the service."""
    stub = _Stub()
    _persist(
        stub, question="q", answer="a", citations=[],
        unsupported=[f"s{i}" for i in range(40)] + [{"not": "a string"}],
    )
    marks = _tutor_turn(stub)["unsupported"]
    assert len(marks) == MAX_UNSUPPORTED_PER_TURN
    assert all(isinstance(m, str) for m in marks)


@pytest.mark.parametrize("junk", [None, "a string", 42, {"a": 1}])
def test_a_malformed_unsupported_field_never_loses_the_turn(junk):
    stub = _Stub()
    out = _persist(stub, question="q", answer="a", citations=[], unsupported=junk)
    assert out["saved"] is True
    assert _tutor_turn(stub)["unsupported"] == []


# --- unchanged behaviour ----------------------------------------------------


def test_an_empty_question_or_answer_is_still_refused():
    for payload in ({"question": "", "answer": "a"}, {"question": "q", "answer": "  "}):
        stub = _Stub()
        assert _persist(stub, **payload) == {"saved": False}
        assert stub.history == []


def test_the_history_window_still_trims():
    """The marks add bytes per turn, so the existing bound has to still apply."""
    stub = _Stub()
    for i in range(HISTORY_WINDOW_TURNS + 5):
        _persist(stub, question=f"q{i}", answer=f"a{i}", citations=[],
                 unsupported=["x" * 100])
    assert len(stub.history) == HISTORY_WINDOW_TURNS * 2


def test_old_turns_without_the_key_are_left_alone():
    """Reading side, compatibility: history written before this change has no
    `unsupported` key. Persisting a new turn must not rewrite or drop them."""
    legacy = [
        {"role": "student", "content": "old q"},
        {"role": "tutor", "content": "old a", "citations": []},
    ]
    stub = _Stub(legacy)
    _persist(stub, question="new q", answer="new a", citations=[], unsupported=["m"])

    assert "unsupported" not in stub.history[1], "a legacy turn was rewritten"
    assert stub.history[1]["content"] == "old a"
    assert stub.history[-1]["unsupported"] == ["m"]
