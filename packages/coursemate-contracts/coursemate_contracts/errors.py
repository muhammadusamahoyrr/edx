"""Honest failure states.

Design §5.1 argues that the difference between *looks broken* and *tells you what
is happening* is the difference between a dead demo and a live one. Three states
that a naive implementation would collapse into one generic error:

    ABSTAINED   retrieval confidence below tau (§8.5) — we know, and we are saying so
    PREPARING   the course has no index yet; a bootstrap has been enqueued (§5.1)
    UNAVAILABLE both hosted providers are down (§8.4) — never a fabricated answer

They are typed here, in the shared contract, so neither side can render them
identically by accident.

**One of these is declared and not yet produced** — `CONTRACT_MISMATCH`, marked
below. That is deliberate and it is enforced:
`tests/test_error_contract.py` holds the pair in an allowlist and fails if either
gains a producer while the browser still has no wording for it, and equally if
any other code loses its producer or its message. A declared code with neither
end wired is a promise the type makes that nothing keeps.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    # --- states that are correct behaviour, not faults ------------------------
    ABSTAINED = "abstained"
    PREPARING = "preparing"

    # --- states that are faults, reported honestly ----------------------------
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    #: Raised before the provider call when a student has spent their daily
    #: token ceiling for this course. See `budget.DailyTokenLedger`.
    BUDGET_EXCEEDED = "budget_exceeded"

    # --- states that mean someone is doing something wrong --------------------
    UNAUTHENTICATED = "unauthenticated"
    NOT_ENROLLED = "not_enrolled"
    #: NOT PRODUCED. Reserved for service/platform version skew, which this
    #: deployment cannot have: both ship from one repo and the XBlock pins no
    #: service version, so there is nothing to compare and disagree about.
    CONTRACT_MISMATCH = "contract_mismatch"


#: Codes that must never be rendered to a student as an error. They are answers.
NOT_A_FAULT: frozenset[ErrorCode] = frozenset(
    {ErrorCode.ABSTAINED, ErrorCode.PREPARING}
)


class ErrorResponse(BaseModel):
    code: ErrorCode
    #: Shown to the student verbatim. Written per-code, never a stack trace.
    message: str
    #: Present on PREPARING so the UI can say "try again shortly" with a number.
    retry_after_seconds: int | None = Field(default=None, ge=0)

    @property
    def is_fault(self) -> bool:
        return self.code not in NOT_A_FAULT
