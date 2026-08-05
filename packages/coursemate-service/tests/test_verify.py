"""Claim verification — the check that gives citations a meaning.

Every test here is written so it CAN fail. The easy mistake in a support checker
is one that flags nothing (looks calm, catches nothing) or flags everything
(looks vigilant, gets ignored); both pass a test that only asserts "it ran".
"""

from __future__ import annotations

import pytest

from coursemate_service.ai.verify import (
    MIN_TERMS,
    content_terms,
    supporting_chunks,
    unsupported_sentences,
)

CONTEXT = [
    "A deadlock occurs when two processes each hold a lock the other needs and "
    "neither can proceed. Ordering lock acquisition consistently prevents it.",
    "Cohorts group learners so that content can be targeted to a subset of a "
    "course without duplicating the course itself.",
]


def _sentences(answer, threshold=0.4):
    return [u.sentence for u in unsupported_sentences(answer, CONTEXT, threshold)]


# --- the thing it must catch ----------------------------------------------

def test_a_sentence_about_unretrieved_material_is_flagged():
    """The actual failure: the model falls back on its own knowledge and says
    something fluent about a topic we never retrieved."""
    answer = ("A deadlock happens when processes wait on each other. "
              "Kubernetes schedules replica pods across availability zones.")
    flagged = _sentences(answer)
    assert len(flagged) == 1
    assert "Kubernetes" in flagged[0]


def test_grounded_sentences_are_not_flagged():
    """Over-flagging is its own failure — a marker on everything trains students
    to ignore the marker."""
    answer = ("A deadlock occurs when two processes each hold a lock the other "
              "needs. Ordering lock acquisition consistently prevents it.")
    assert _sentences(answer) == []


def test_synthesis_across_two_chunks_is_supported():
    """Scoring per chunk instead of against the union would flag exactly the
    good synthesis we want the tutor to do."""
    answer = "Cohorts group learners, and deadlock occurs when processes hold locks."
    assert _sentences(answer) == []


# --- the things it must NOT flag ------------------------------------------

def test_short_connective_sentences_are_skipped():
    answer = "Yes. In short: a deadlock occurs when two processes hold locks. Hope that helps."
    assert _sentences(answer) == []


def test_questions_are_never_flagged():
    """Socratic mode opens with a guiding question by design. Flagging the
    tutor's own question as an unsupported claim would be wrong and very
    visible."""
    answer = "What might happen if two processes each hold a lock forever in Kubernetes?"
    assert _sentences(answer) == []


def test_no_context_means_no_verdict():
    """With nothing retrieved there is nothing to check against, and flagging
    every sentence would be an assertion we cannot support either."""
    assert unsupported_sentences("Anything at all here.", [], 0.4) == []


# --- threshold behaviour ---------------------------------------------------

def test_threshold_moves_the_line():
    answer = "Deadlock relates to Kubernetes scheduling and pods."
    assert _sentences(answer, threshold=0.1) == []      # lenient: passes
    assert _sentences(answer, threshold=0.9) != []      # strict: flagged


# --- citation narrowing ----------------------------------------------------

def test_citations_narrow_to_the_chunks_actually_used():
    """The D2 fix: a citation should mean 'the answer used this', not 'we
    searched this'."""
    answer = "A deadlock occurs when two processes hold locks the other needs."
    assert supporting_chunks(answer, CONTEXT) == [0]     # not the cohorts chunk


def test_citations_fall_back_to_all_when_nothing_overlaps():
    """§8.5 makes citation mandatory. Dropping to zero would break that in the
    one case where the student most needs to see what the answer rested on."""
    assert supporting_chunks("Zzzz qqqq wwww.", CONTEXT) == [0, 1]


# --- tokenizer -------------------------------------------------------------

def test_stemming_matches_singular_and_plural():
    """'cohorts' in the answer must match 'cohort' in the lesson, or the check
    flags correct answers for grammatical reasons."""
    assert content_terms("cohorts") & content_terms("cohort")


def test_min_terms_is_actually_enforced():
    short = " ".join(["zzz"] * (MIN_TERMS - 1))
    assert unsupported_sentences(f"{short}.", CONTEXT, 0.9) == []
