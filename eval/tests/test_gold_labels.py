"""The gold set is the measuring instrument, so changes to it get a test.

Every other test here checks whether the system does the right thing. These check
whether the *labels* still say what they said when the numbers were published —
because the cheapest way to improve a benchmark is to relabel the cases it fails,
and that change looks identical in a diff to genuinely fixing a wrong label.

Only `t02` is pinned so far, because it is the only label that has actually been
argued about. See the comment above the case in `retrieval_gold.yaml`.
"""

from __future__ import annotations

import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVAL))

from run_eval import load_dataset

#: Retrieved at 0.833 by `t02` and repeatedly mistaken for a correct hit on the
#: strength of its name. Its whole indexed body is a note telling the reader to
#: install the forum plugin.
NOT_AN_ANSWER = "Duplicate of 'Discussion Prompt'"


def t02() -> dict:
    questions = load_dataset(EVAL / "datasets" / "retrieval_gold.yaml")["questions"]
    by_id = {q["id"]: q for q in questions}
    assert "t02" in by_id, "t02 was removed from the gold set"
    return by_id["t02"]


def test_t02_does_not_accept_the_configuration_note():
    """Resolved 2026-08-12: `t02` is a real retrieval failure, not a bad label.

    The block is named as if it duplicated `Discussion Prompt`. It does not — it
    contains only:

        "Configuration Note: If you cannot access the inline discussions or see
         the Discussions tab, then you likely need to install the Open edX LMS
         Forum Plugin. Work with your site administrator to do this, if needed."

    A student who asks how discussions work and is handed that has been sent to
    troubleshoot an installation. Counting it correct would mean the benchmark
    rewards retrieving the right *topic* rather than an answer, and it would have
    moved topic_change wrong-and-answered from 1 to 0 — an improvement produced
    entirely by editing this file.

    If a future change makes this block genuinely answer the question — the
    content is rewritten, or chunking merges it with something explanatory — then
    delete this test in the same commit that shows the new text. Do not delete it
    to make a run look better.
    """
    assert NOT_AN_ANSWER not in t02()["expect"]


def test_t02_still_expects_the_block_that_does_answer_it():
    """The other half. A label can also be broken by emptying it — dropping the
    expectation makes the miss disappear just as effectively as widening it."""
    assert "How do discussions work?" in t02()["expect"]


def test_t02_is_still_a_topic_change_case():
    """Its arm is what makes it load-bearing: it is the case that proves history
    must NOT be prepended to a self-contained question. Reclassifying it would
    quietly remove that evidence."""
    case = t02()
    assert case["arm"] == "topic_change"
    assert case["history"] == ["What are video transcripts used for?"]
