"""`ai.query.retrieval_query` — what gets searched for.

The function exists because retrieval used to search on the bare question, so a
follow-up carried no topic. These tests pin three things the measurement paid
for:

* a single-turn question is passed through **untouched**, so the arms that were
  already working cannot move
* a follow-up is reconstructed from the conversation
* `usage_key` cannot override the conversation — pinned now, before any consumer
  exists, so the decision is enforced by a test rather than by whoever writes it
"""

from __future__ import annotations

import pytest
from coursemate_contracts.chat import Role, Turn
from coursemate_service.ai.query import (
    is_under_specified,
    last_student_turn,
    retrieval_query,
)

KEY_A = "block-v1:OpenedX+DemoX+DemoCourse+type@html+block@0f594fe7afb947bbbac7"
KEY_B = "block-v1:OpenedX+DemoX+DemoCourse+type@html+block@5dd796da0de64ed782b0"


def student(text: str) -> Turn:
    return Turn(role=Role.STUDENT, content=text)


def tutor(text: str) -> Turn:
    return Turn(role=Role.TUTOR, content=text)


# --- the no-history path must not move ------------------------------------


@pytest.mark.parametrize("q", [
    "What is a cohort?",
    "How do I set up cohorts in my course?",
    "What helps deaf learners follow along with a recorded lecture?",
])
def test_a_first_turn_is_searched_exactly_as_typed(q):
    """The `original` and `paraphrase` arms were measured on the bare question.
    If this changed, those baselines would move for a reason unrelated to the
    thing being fixed, and the comparison would be worthless."""
    assert retrieval_query(q, []) == q


def test_empty_history_ignores_usage_key_too():
    """B1 does not consume page context. Stated as a test so that when it is
    wired, the change is deliberate and visible rather than incidental."""
    assert retrieval_query("What is a cohort?", [], KEY_A) == "What is a cohort?"


def test_whitespace_is_trimmed_not_preserved():
    assert retrieval_query("  What is a cohort?  ", []) == "What is a cohort?"


# --- the follow-up path ----------------------------------------------------


def test_a_follow_up_carries_the_previous_question():
    """The defect this function exists for. "why would I use one?" alone is
    unsearchable — measured against the live index it returned an unrelated
    numerical-input problem at 0.675, above the threshold, and was answered."""
    q = retrieval_query("Why would I use one?", [student("What is a cohort?")])
    assert q == "What is a cohort? Why would I use one?"


def test_the_student_turn_is_used_not_the_tutor_reply():
    """Searching on our own previous answer would feed the retriever whatever the
    model said — including, on a bad turn, content drawn from the wrong lesson.
    Only what the student typed is evidence of what they are asking about."""
    history = [
        student("What is a cohort?"),
        tutor("A cohort is a group of learners who see the same content."),
    ]
    assert (retrieval_query("Who grades it?", history)
            == "What is a cohort? Who grades it?")


def test_only_the_most_recent_student_turn_is_used():
    """One turn, not the whole window. It bounds how long the query can grow, and
    the measurement says one is enough."""
    history = [
        student("What are XBlocks?"),
        tutor("XBlocks are the components a course is built from."),
        student("What is a cohort?"),
        tutor("A cohort is a group of learners."),
    ]
    q = retrieval_query("why would I use one?", history)
    assert q == "What is a cohort? why would I use one?"
    assert "XBlocks" not in q


def test_history_of_only_tutor_turns_is_no_history():
    """Nothing the student said means nothing to reconstruct from."""
    assert (retrieval_query("Who grades it?", [tutor("Here is an answer.")])
            == "Who grades it?")


def test_a_blank_student_turn_is_skipped():
    history = [student("What is a cohort?"), student("   ")]
    assert (retrieval_query("Who grades it?", history)
            == "What is a cohort? Who grades it?")


def test_a_topic_change_still_carries_its_own_terms():
    """A self-contained new question keeps every one of its own words. The
    previous turn is added, never substituted — so the retriever still has the
    new topic to match on."""
    q = retrieval_query("What are XBlocks?", [student("What is a cohort?")])
    assert "XBlocks" in q
    assert q.endswith("What are XBlocks?")


# --- the precedence rule, pinned before there is a consumer ---------------


def test_usage_key_cannot_change_the_query_when_history_exists():
    """**The rule this signature exists to enforce.** The page is where the
    student happens to be standing; the conversation is what they are asking
    about, and the two disagree the moment a student carries a question to a
    different page. A future change that lets page context win will fail here."""
    history = [student("What is a cohort?")]
    assert (retrieval_query("Why would I use one?", history, KEY_A)
            == retrieval_query("Why would I use one?", history, KEY_B)
            == retrieval_query("Why would I use one?", history, None))


def test_the_function_is_pure():
    """No I/O, no clock, no model — so it can be exercised exhaustively. Same
    inputs, same output, and the inputs are not mutated."""
    history = [student("What is a cohort?")]
    before = [(t.role, t.content) for t in history]
    first = retrieval_query("Who grades it?", history)
    second = retrieval_query("Who grades it?", history)
    assert first == second
    assert [(t.role, t.content) for t in history] == before


# --- the helper ------------------------------------------------------------


def test_last_student_turn_finds_the_most_recent():
    history = [student("first"), tutor("reply"), student("second")]
    assert last_student_turn(history) == "second"


def test_last_student_turn_is_none_when_there_is_none():
    assert last_student_turn([]) is None
    assert last_student_turn([tutor("only a reply")]) is None


# --- the under-specification guard (B2) ------------------------------------


@pytest.mark.parametrize("q", [
    "Why would I use one?",
    "How do I set one up?",
    "How do I create them?",
    "Who grades it?",
    "How are they different from cohorts?",
    "What decides who sees it?",
    "Can I moderate them?",
    "What comes below it?",
    "How do I try it out?",
    "Is there something similar for longer feedback?",
])
def test_a_question_leaning_on_a_pro_form_is_under_specified(q):
    """Strip the conversation away and there is nothing left to search for."""
    assert is_under_specified(q) is True


@pytest.mark.parametrize("q", [
    "What are XBlocks?",
    "How do discussions work?",
    "What is a content library?",
    "What is SCORM?",
    "What is a cohort?",
    "How do I set up cohorts in my course?",
])
def test_a_self_contained_question_is_not(q):
    """These carry their own topic. B1 measured what happens when they are
    augmented anyway: topic-change r@1 fell from 0.750 to 0.250."""
    assert is_under_specified(q) is False


def test_a_self_contained_question_is_never_augmented():
    """The whole point of B2. A topic change keeps its own query even mid
    conversation."""
    history = [student("What is a cohort?")]
    assert retrieval_query("What is SCORM?", history) == "What is SCORM?"


def test_a_follow_up_mid_conversation_still_is():
    history = [student("How do discussions work?")]
    assert (retrieval_query("Can I moderate them?", history)
            == "How do discussions work? Can I moderate them?")


def test_the_guard_runs_before_history_is_consulted():
    """A self-contained question is returned unchanged whether or not there is a
    conversation behind it — so the two arms cannot interact."""
    q = "What is SCORM?"
    assert retrieval_query(q, []) == retrieval_query(q, [student("What is a cohort?")]) == q


def test_pro_forms_are_matched_as_words_not_substrings():
    """"it" inside "monitor" or "editing" must not trigger augmentation."""
    assert is_under_specified("How do I use the editing monitor?") is False
    assert is_under_specified("Who grades it?") is True


def test_case_and_punctuation_do_not_matter():
    assert is_under_specified("WHO GRADES IT?") is True
    assert is_under_specified("Can I moderate them???") is True


def test_ellipsis_without_a_pro_form_is_a_known_gap():
    """`m05` — "Can you give an example?" is under-specified by ELLIPSIS, not
    anaphora: there is no pronoun to find. Catching it needs to know which nouns
    are topical in this corpus, which is a different signal and is deliberately
    not attempted here.

    Pinned as a test so the limit is documented rather than discovered."""
    assert is_under_specified("Can you give an example?") is False


def test_a_bare_interrogative_is_also_a_known_gap():
    """"why?" carries no pro-form, so the guard does not fire and the question is
    searched as-is. It is the same ellipsis gap as `m05`, and it is recorded
    rather than patched: adding bare question words to the pro-form list would
    start turning a closed grammatical class into a list of phrasings someone
    observed.

    It fails SAFELY, which is why it is tolerable. Measured against the live
    index, "why?" scores 0.000 — far below tau — so the pipeline abstains rather
    than answering from whatever it happened to match. That is the opposite of
    `m05`, which scores 0.375 and answers.
    """
    assert is_under_specified("why?") is False
    assert retrieval_query("why?", [student("What is a cohort?")]) == "why?"


# --- the shape a real browser actually sends -------------------------------


def test_the_current_question_inside_history_is_not_treated_as_the_previous_turn():
    """**The defect browser verification found on 2026-08-12.**

    `tutor.js` pushes the question onto its history array before building the
    request, so the wire payload carries the current question in BOTH fields.
    Captured verbatim from a real request:

        {"question": "Why would I use one?",
         "history": [{"role":"student","content":"What is a cohort?"},
                     {"role":"tutor","content":"A cohort is ..."},
                     {"role":"student","content":"Why would I use one?"}]}

    Before the fix this produced `"Why would I use one? Why would I use one?"` —
    the question doubled, the conversation never consulted, and B1/B2 a complete
    no-op in production while every unit test and the offline eval passed. They
    passed because they build history the way the contract READS, with prior
    turns only. A harness that constructs its own input cannot discover what a
    real client sends.
    """
    wire = [
        student("What is a cohort?"),
        tutor("A cohort is a course-level group of learners..."),
        student("Why would I use one?"),
    ]
    assert (retrieval_query("Why would I use one?", wire)
            == "What is a cohort? Why would I use one?")


def test_history_that_omits_the_current_question_still_works():
    """The contract's own reading, which the eval harness uses. Both shapes must
    produce the same query or the offline numbers mean nothing."""
    contract_shape = [student("What is a cohort?")]
    wire_shape = [student("What is a cohort?"), student("Why would I use one?")]
    assert (retrieval_query("Why would I use one?", contract_shape)
            == retrieval_query("Why would I use one?", wire_shape))


def test_a_question_repeated_verbatim_falls_back_to_the_turn_before_it():
    """A student who asks the same thing twice is not giving us a new topic, so
    the reconstruction reaches past both copies rather than augmenting with an
    identical string."""
    history = [
        student("What is a cohort?"),
        student("Why would I use one?"),
        student("Why would I use one?"),
    ]
    assert (retrieval_query("Why would I use one?", history)
            == "What is a cohort? Why would I use one?")


def test_only_the_current_question_in_history_leaves_it_unchanged():
    """First turn, echoed into history by the browser. There is no conversation
    to reconstruct from, so the question is searched as typed."""
    assert retrieval_query("why would I use one?",
                           [student("why would I use one?")]) == "why would I use one?"
