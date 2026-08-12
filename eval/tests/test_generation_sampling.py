"""Generation must never sample a conversational case.

`run_generation` runs the pipeline on `question` alone, with no history
attached. Sampling *"Why would I use one?"* would therefore measure how well the
model copes with a question stripped of its conversation — which is not what the
benchmark reports, and the number would look like a generation-quality result.

Until 2026-08-12 this held only by accident: the sample took the first N covered
cases positionally and the 18 conversational cases happened to be appended last.
Reordering the gold set, or raising `--gen` past the single-turn cases, would
have started generating answers to bare follow-ups and reporting them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVAL))

from harness.runner import (
    GENERATION_ARMS,
    build_query,
    generation_candidates,
)
from run_eval import load_dataset

CONVERSATIONAL = ("multiturn", "topic_change", "usage_key_conflict")


def case(cid, arm, question="q?", history=None, usage_key=None):
    return {
        "id": cid, "arm": arm, "question": question,
        "history": history or [], "usage_key": usage_key,
        "expect": ["X"], "covered": True,
    }


def test_only_single_turn_arms_are_eligible():
    mixed = [
        case("q01", "original"),
        case("p01", "paraphrase"),
        case("m01", "multiturn", history=["What is a cohort?"]),
        case("t01", "topic_change", history=["What is a cohort?"]),
        case("u01", "usage_key_conflict", history=["What is a cohort?"], usage_key="block-v1:x"),
    ]
    assert [c["id"] for c in generation_candidates(mixed)] == ["q01", "p01"]


@pytest.mark.parametrize("arm", CONVERSATIONAL)
def test_every_conversational_arm_is_excluded(arm):
    assert generation_candidates([case("x", arm, history=["prior"])]) == []


def test_the_real_gold_set_yields_no_conversational_case():
    """Against the shipped dataset, not a fixture — a fixture proves the filter
    works on data the filter's author invented."""
    questions = load_dataset(EVAL / "datasets" / "retrieval_gold.yaml")["questions"]
    eligible = generation_candidates(questions)

    assert eligible, "the filter removed everything"
    assert all(q["arm"] in GENERATION_ARMS for q in eligible)
    assert not [q["id"] for q in eligible if q["history"]], (
        "a case carrying history reached generation sampling"
    )


def test_position_in_the_file_cannot_smuggle_a_case_through():
    """The bug this guards. The old sampler took the first N covered cases, so
    safety depended on where the conversational arms happened to sit in the
    YAML. Reversing the order must change nothing."""
    questions = load_dataset(EVAL / "datasets" / "retrieval_gold.yaml")["questions"]
    forward = {q["id"] for q in generation_candidates(questions)}
    reversed_ = {q["id"] for q in generation_candidates(list(reversed(questions)))}
    assert forward == reversed_


def test_an_unlabelled_case_defaults_to_eligible():
    """Every case predating the `arm:` field is single-turn, so the default must
    keep it in the sample — otherwise adding the field would silently shrink the
    generation set."""
    legacy = {"id": "old", "question": "What is a cohort?", "expect": ["Cohorts"],
              "covered": True, "history": [], "usage_key": None}
    assert generation_candidates([legacy]) == [legacy]


# --- the shared query builder ---------------------------------------------


def test_there_is_exactly_one_query_builder():
    """Both harnesses must call `harness.runner.build_query`, not keep copies.

    `run_conversation_eval.py` briefly had its own identical implementation. Two
    copies that agree today are two copies that can disagree tomorrow, and each
    harness would look self-consistent while reporting different numbers for the
    same case.
    """
    source = (EVAL / "run_conversation_eval.py").read_text(encoding="utf-8")
    assert "def build_query" not in source, (
        "run_conversation_eval.py has its own build_query again"
    )
    assert "from harness.runner import build_query" in source


def test_the_shared_builder_reconstructs_a_follow_up():
    c = case("m01", "multiturn", "Why would I use one?", ["What is a cohort?"])
    assert build_query(c) == "What is a cohort? Why would I use one?"


def test_build_query_leaves_a_single_turn_case_alone():
    c = case("q01", "original", "What is a cohort?")
    assert build_query(c) == "What is a cohort?"


def test_build_query_tolerates_a_legacy_case_with_no_history_key():
    """The dataset gained `history` in Phase A; anything constructing a case dict
    by hand may not set it."""
    assert build_query({"question": "What is a cohort?"}) == "What is a cohort?"
