"""The confidence gate — one implementation, every caller (§8.5).

This was three lines inline in `pipeline.py` while there was one retrieval path.
The exam-prep agent adds more: decision 6 in the agent design says *each retrieval
tool applies the existing lexical gate to its own results*, and "the existing gate"
has to mean the same code, not a second copy that agrees today.

A second copy is not a hypothetical risk here. The gate compares against the
**blended** rerank score, not the raw coverage the store computes — a distinction
that already cost this project one bug and is documented at length in
`config.confidence_threshold` and `knowledge/store.py`. A reimplementation that
compared the wrong number would abstain differently, on questions nobody tested,
and both paths would look correct in isolation.

**Nothing about the decision changes here.** The threshold, the comparison, and
the ordering of the checks are lifted verbatim. `require_grounding` still governs
whether the gate applies at all, and it governs it identically on both paths — a
flag that meant one thing for chat and another for the agent would be worse than
no flag.
"""

from __future__ import annotations

from enum import StrEnum

from coursemate_contracts.errors import ErrorCode

from ..config import settings
from .context import ContextResult


class GateOutcome(StrEnum):
    """Why the gate decided what it decided.

    Three outcomes, not a boolean, because two of the three failures are not the
    same thing to a student (§5.1): "this course is still being prepared" invites
    them back, "not covered in this course" does not.
    """

    PASS = "pass"
    #: The offering has no index at all. Distinct from "nothing matched".
    NO_INDEX = "no_index"
    #: Nothing was retrieved, or the best score fell below tau.
    BELOW_THRESHOLD = "below_threshold"


#: What each outcome means on the wire. Kept beside the enum so a new outcome
#: cannot be added without deciding what the student is told.
ERROR_CODE: dict[GateOutcome, ErrorCode | None] = {
    GateOutcome.PASS: None,
    GateOutcome.NO_INDEX: ErrorCode.PREPARING,
    GateOutcome.BELOW_THRESHOLD: ErrorCode.ABSTAINED,
}


def evaluate(result: ContextResult) -> GateOutcome:
    """Gate one retrieval result.

    Order matters and is unchanged: the missing-index check runs *before* the
    score check, because an empty index trivially scores 0.0 and would otherwise
    be reported as "not covered in this course" — telling a student the material
    does not exist when it has simply not been ingested yet.
    """
    if not settings.require_grounding:
        return GateOutcome.PASS
    if result.index_missing:
        return GateOutcome.NO_INDEX
    if result.is_empty or result.top_score < settings.confidence_threshold:
        return GateOutcome.BELOW_THRESHOLD
    return GateOutcome.PASS
