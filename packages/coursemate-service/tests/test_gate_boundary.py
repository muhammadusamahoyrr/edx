"""Where exactly the confidence gate draws its line.

The gate is the control the whole honesty argument rests on: below the bar the
tutor says "not covered" instead of answering from the model's own knowledge. It
had no test of its own boundary — the threshold was exercised only indirectly,
through pipeline and generator tests that set tau far from the score to force an
outcome, which cannot see where the line actually falls.

That gap let a contradiction live: `config.abstain_on_tie` defaulted True with a
comment saying "we tune toward abstention", while `gate.evaluate` compares
`top_score < threshold`, so a tie ANSWERS. The setting was read by nothing, so
the code was right and the configuration was a claim a reader would believe.
The setting is gone; these tests are what keep the rule from drifting back.

Deliberately no test asserts a particular value of tau. Tau is calibrated
(0.35, n=28) and lives in BENCHMARKS; pinning it here would mean two places to
change it and one of them forgotten.
"""

from __future__ import annotations

import pytest
from coursemate_contracts.chat import Citation
from coursemate_contracts.errors import ErrorCode
from coursemate_service.ai import gate
from coursemate_service.ai.context import ContextChunk, ContextResult

TAU = 0.50  # local, arbitrary: these tests are about the comparison, not the value


@pytest.fixture(autouse=True)
def _grounding_on(monkeypatch):
    monkeypatch.setattr(gate.settings, "require_grounding", True)
    monkeypatch.setattr(gate.settings, "confidence_threshold", TAU)


def result(score: float) -> ContextResult:
    return ContextResult(
        chunks=[ContextChunk(text="t", citation=Citation(usage_key="u"), score=score)],
        top_score=score,
        index_missing=False,
    )


# --- the boundary ----------------------------------------------------------


def test_below_tau_abstains():
    assert gate.evaluate(result(TAU - 0.01)) is gate.GateOutcome.BELOW_THRESHOLD


def test_exactly_tau_passes():
    """**The tie answers.** `top_score < threshold` means reaching the floor is
    meeting it. This is the assertion `abstain_on_tie = True` contradicted."""
    assert gate.evaluate(result(TAU)) is gate.GateOutcome.PASS


def test_above_tau_passes():
    assert gate.evaluate(result(TAU + 0.01)) is gate.GateOutcome.PASS


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, gate.GateOutcome.BELOW_THRESHOLD),
        (TAU - 1e-9, gate.GateOutcome.BELOW_THRESHOLD),
        (TAU, gate.GateOutcome.PASS),
        (TAU + 1e-9, gate.GateOutcome.PASS),
        (1.0, gate.GateOutcome.PASS),
    ],
)
def test_the_line_falls_exactly_at_tau(score, expected):
    assert gate.evaluate(result(score)) is expected


# --- what the student is told ----------------------------------------------


def test_below_tau_is_reported_as_abstained_not_as_a_fault():
    outcome = gate.evaluate(result(TAU - 0.01))
    assert gate.ERROR_CODE[outcome] is ErrorCode.ABSTAINED


def test_a_pass_carries_no_error_code():
    assert gate.ERROR_CODE[gate.evaluate(result(TAU))] is None


# --- the checks that must keep running in order ----------------------------


def test_an_empty_result_abstains_whatever_the_score_says():
    empty = ContextResult(chunks=[], top_score=0.9, index_missing=False)
    assert gate.evaluate(empty) is gate.GateOutcome.BELOW_THRESHOLD


def test_a_missing_index_is_preparing_not_abstained():
    """Order matters: an empty index scores 0.0 and would otherwise be reported
    as 'not covered in this course' — telling a student the material does not
    exist when it has simply not been ingested."""
    missing = ContextResult(chunks=[], top_score=0.0, index_missing=True)
    assert gate.evaluate(missing) is gate.GateOutcome.NO_INDEX
    assert gate.ERROR_CODE[gate.evaluate(missing)] is ErrorCode.PREPARING


def test_grounding_off_passes_everything(monkeypatch):
    """The flag still short-circuits ahead of every other check."""
    monkeypatch.setattr(gate.settings, "require_grounding", False)
    assert gate.evaluate(result(0.0)) is gate.GateOutcome.PASS
    assert gate.evaluate(ContextResult(chunks=[], top_score=0.0, index_missing=True)) \
        is gate.GateOutcome.PASS


# --- the setting is gone, and must stay gone -------------------------------


def test_abstain_on_tie_is_not_a_setting_again():
    """It was declared, read by nothing, and stated the opposite of the rule
    above. If it returns, it must be BECAUSE something reads it — and then these
    boundary tests are what say which direction it selects."""
    from coursemate_service.config import settings

    assert not hasattr(settings, "abstain_on_tie")
