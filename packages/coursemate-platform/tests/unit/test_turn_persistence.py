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
    MAX_CONVERSATIONS,
    PRACTICE_WINDOW,
    CourseMateTutorXBlock,
)


class _Stub:
    """Enough of the block for `persist_turn`.

    The conversation helpers are borrowed from the real class rather than
    reimplemented: they decide which conversation a legacy turn belongs to, and a
    stub that answered that differently would make every backward-compatibility
    test below agree with itself instead of with the block.
    """

    # `staticmethod(...)` restored explicitly: reading it off the class yields the
    # plain function, and assigning that in a class body would rebind it as an
    # instance method — so it would receive `self` as its first argument.
    _conversation_of = staticmethod(CourseMateTutorXBlock._conversation_of)
    _turns_in = CourseMateTutorXBlock._turns_in
    _conversations = CourseMateTutorXBlock._conversations
    _active_id = CourseMateTutorXBlock._active_id
    _title_for = CourseMateTutorXBlock._title_for
    _conversation_list = CourseMateTutorXBlock._conversation_list

    def __init__(self, history=None, conversations=None, active=""):
        self.history = list(history or [])
        self.conversations = list(conversations or [])
        self.active_conversation = active
        self.practice = []


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
    # `conversation_id` is written on every new turn as of E3; `""` is the
    # default conversation, which is where a student with one chat stays.
    assert student == {"role": "student", "content": "What is a content group?",
                       "conversation_id": ""}
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


# --- E3: several conversations, and the old data that predates them ---------
#
# The compatibility case is not hypothetical: every turn already stored for every
# student was written without `conversation_id`. If those stop loading, the
# feature has destroyed the thing it was meant to extend.


def _call(name, stub, **payload):
    fn = getattr(CourseMateTutorXBlock, name).__wrapped__
    return fn(stub, payload)


LEGACY = [
    {"role": "student", "content": "what is a cohort?"},
    {"role": "tutor", "content": "A cohort is...", "citations": []},
]


def test_turns_written_before_e3_still_load():
    """No `conversation_id`, exactly as they sit in production today."""
    stub = _Stub(history=list(LEGACY))
    assert stub._turns_in("") == LEGACY
    assert stub._active_id() == ""


def test_legacy_turns_appear_as_an_openable_conversation():
    stub = _Stub(history=list(LEGACY))
    listed = stub._conversation_list()
    assert [c["id"] for c in listed] == [""]
    assert listed[0]["turns"] == 2


def test_a_student_with_no_history_lists_nothing():
    assert _Stub()._conversation_list() == []


def test_a_new_turn_joins_the_active_conversation():
    stub = _Stub(history=list(LEGACY), conversations=[{"id": "c1", "title": "New chat"}],
                 active="c1")
    _persist(stub, question="q", answer="a")

    assert len(stub._turns_in("")) == 2, "the legacy turns moved"
    assert len(stub._turns_in("c1")) == 2


def test_legacy_turns_survive_a_new_conversation():
    stub = _Stub(history=list(LEGACY))
    out = _call("new_conversation", stub)

    assert out["conversation_id"]
    assert stub._turns_in("") == LEGACY, "starting a new chat destroyed the old one"
    assert {c["id"] for c in stub._conversation_list()} == {"", out["conversation_id"]}


def test_a_conversation_is_named_after_its_first_question():
    stub = _Stub(history=[{"role": "student", "content": "what is a cohort?",
                           "conversation_id": "c1"}],
                 conversations=[{"id": "c1", "title": "New chat"}], active="c1")
    assert stub._title_for("c1") == "what is a cohort?"


def test_switching_returns_that_conversation_only():
    stub = _Stub(
        history=[*LEGACY,
                 {"role": "student", "content": "second chat", "conversation_id": "c1"}],
        conversations=[{"id": "c1", "title": "New chat"}], active="c1")

    out = _call("switch_conversation", stub, conversation_id="")
    assert [t["content"] for t in out["history"]] == [t["content"] for t in LEGACY]
    assert stub.active_conversation == ""


def test_switching_to_an_unknown_conversation_is_refused():
    stub = _Stub(history=list(LEGACY))
    assert "error" in _call("switch_conversation", stub, conversation_id="nope")


def test_clearing_removes_only_the_active_conversation():
    stub = _Stub(
        history=[*LEGACY,
                 {"role": "student", "content": "second", "conversation_id": "c1"}],
        conversations=[{"id": "c1", "title": "New chat"}], active="c1")

    _call("clear_history", stub)
    assert stub._turns_in("") == LEGACY, "clearing one chat emptied another"
    assert stub._turns_in("c1") == []


def test_clearing_never_touches_practice():
    stub = _Stub(history=list(LEGACY))
    stub.practice = [{"attempt_id": "a1", "text": "Q?"}]
    _call("clear_history", stub)
    assert stub.practice == [{"attempt_id": "a1", "text": "Q?"}]


def test_the_window_trims_per_conversation_not_across_the_list():
    """A busy chat must not evict another one's turns."""
    stub = _Stub(history=[{"role": "student", "content": "keep me",
                           "conversation_id": "quiet"}],
                 conversations=[{"id": "quiet", "title": "q"},
                                {"id": "busy", "title": "b"}], active="busy")
    for i in range(HISTORY_WINDOW_TURNS + 5):
        _persist(stub, question=f"q{i}", answer=f"a{i}")

    assert len(stub._turns_in("quiet")) == 1, "the quiet conversation was trimmed away"
    assert len(stub._turns_in("busy")) == HISTORY_WINDOW_TURNS * 2


def test_a_dropped_conversation_takes_its_turns_with_it():
    """Otherwise `history` grows forever with turns nothing can open."""
    stub = _Stub(conversations=[{"id": f"c{i}", "title": "x"}
                                for i in range(MAX_CONVERSATIONS)])
    stub.history = [{"role": "student", "content": "old", "conversation_id": "c0"}]

    _call("new_conversation", stub)
    assert stub._turns_in("c0") == []
    assert len(stub.conversations) == MAX_CONVERSATIONS


# --- E2: the practice run survives a reload --------------------------------


def test_a_practice_card_is_stored():
    stub = _Stub()
    out = _call("persist_practice", stub, attempt_id="a1", text="Explain deadlock.",
                question_id="Q1", clo_id="CLO-1", difficulty_band="hard", citations=[])
    assert out["saved"] is True
    assert stub.practice[0]["text"] == "Explain deadlock."
    assert stub.practice[0]["attempt_id"] == "a1"


def test_a_card_without_an_attempt_id_is_refused():
    """The id is what makes one card one attempt; a card without one could be
    answered twice on restore."""
    stub = _Stub()
    assert _call("persist_practice", stub, text="Q?")["saved"] is False
    assert stub.practice == []


def test_re_persisting_a_card_replaces_it_rather_than_duplicating():
    stub = _Stub()
    _call("persist_practice", stub, attempt_id="a1", text="Q?")
    _call("persist_practice", stub, attempt_id="a1", text="Q?", answered=True)

    assert len(stub.practice) == 1
    assert stub.practice[0]["answered"] is True


def test_the_attempt_id_is_carried_not_regenerated():
    """Restoring a card must reuse its id. A fresh one would let the same card be
    answered twice and counted twice."""
    stub = _Stub()
    _call("persist_practice", stub, attempt_id="fixed-id", text="Q?")
    _call("persist_practice", stub, attempt_id="fixed-id", text="Q?", answered=True)
    assert [c["attempt_id"] for c in stub.practice] == ["fixed-id"]


def test_the_practice_run_is_bounded():
    stub = _Stub()
    for i in range(PRACTICE_WINDOW + 6):
        _call("persist_practice", stub, attempt_id=f"a{i}", text=f"Q{i}")
    assert len(stub.practice) == PRACTICE_WINDOW


def test_clearing_practice_leaves_the_chat_alone():
    stub = _Stub(history=list(LEGACY))
    _call("persist_practice", stub, attempt_id="a1", text="Q?")
    _call("clear_practice", stub)
    assert stub.practice == []
    assert stub.history == LEGACY


def test_the_legacy_conversation_survives_eviction():
    """**The one conversation that cannot be recreated.**

    Its turns predate E3 entirely — nothing wrote a `conversation_id` for them
    and nothing can reconstruct one. It is also the OLDEST entry, so a plain
    tail-slice drops it first: a student prolific enough to open
    MAX_CONVERSATIONS chats would silently lose everything they wrote before the
    feature existed.
    """
    stub = _Stub(history=[{"role": "student", "content": "pre-E3 question"}])
    for _ in range(MAX_CONVERSATIONS + 5):
        _call("new_conversation", stub)

    ids = [c["id"] for c in stub._conversation_list()]
    assert "" in ids, f"the legacy conversation was evicted: {ids}"
    assert stub._turns_in("") == [{"role": "student", "content": "pre-E3 question"}]
    assert len(stub.conversations) <= MAX_CONVERSATIONS


def test_ordinary_conversations_still_age_out():
    """Pinning the legacy one must not disable eviction for everything else."""
    stub = _Stub(history=[{"role": "student", "content": "pre-E3"}])
    seen = []
    for _ in range(MAX_CONVERSATIONS + 5):
        seen.append(_call("new_conversation", stub)["conversation_id"])

    ids = {c["id"] for c in stub._conversation_list()}
    assert seen[0] not in ids, "no ordinary conversation was ever evicted"
    assert seen[-1] in ids, "the newest conversation was evicted"


def test_an_evicted_conversation_still_takes_its_turns():
    stub = _Stub()
    first = _call("new_conversation", stub)["conversation_id"]
    stub.history.append({"role": "student", "content": "doomed",
                         "conversation_id": first})
    for _ in range(MAX_CONVERSATIONS + 2):
        _call("new_conversation", stub)

    assert stub._turns_in(first) == [], "orphaned turns were left in history"


def test_clear_history_removes_only_the_active_conversation_and_no_mastery():
    """The delete control's contract, asserted at the handler."""
    stub = _Stub(
        history=[{"role": "student", "content": "keep", "conversation_id": "other"},
                 {"role": "student", "content": "drop", "conversation_id": "mine"}],
        conversations=[{"id": "other", "title": "o"}, {"id": "mine", "title": "m"}],
        active="mine")
    stub.practice = [{"attempt_id": "a1", "text": "Q?"}]

    out = _call("clear_history", stub)

    assert out["conversation_id"] == "mine"
    assert stub._turns_in("other") == [{"role": "student", "content": "keep",
                                        "conversation_id": "other"}]
    assert stub._turns_in("mine") == []
    assert stub.practice == [{"attempt_id": "a1", "text": "Q?"}], "practice was cleared too"


def test_clear_practice_removes_only_practice():
    stub = _Stub(history=[{"role": "student", "content": "keep"}])
    stub.practice = [{"attempt_id": "a1", "text": "Q?"}]

    _call("clear_practice", stub)

    assert stub.practice == []
    assert stub.history == [{"role": "student", "content": "keep"}]
