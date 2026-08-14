"""Honest failure states.

Design §5.1 argues that the difference between *looks broken* and *tells you what
is happening* is the difference between a dead demo and a live one. Three states
that a naive implementation would collapse into one generic error:

    ABSTAINED   retrieval confidence below tau (§8.5) — we know, and we are saying so
    PREPARING   the course has no index yet; a bootstrap has been enqueued (§5.1)
    UNAVAILABLE both hosted providers are down (§8.4) — never a fabricated answer

They are typed here, in the shared contract, so neither side can render them
identically by accident.

**Every code here has a producer, and every code a browser can reach has
wording.** `tests/test_error_contract.py` enforces both directions and its
"declared but unproduced" allowlist is empty — a declared code with neither end
wired is a promise the type makes that nothing keeps.

That test also covers a third vocabulary the enum does not contain: the XBlock's
handlers return plain strings (`disabled`, `forbidden`, `bad_request`,
`invalid_mode`) that reach the same `showNotice` lookup. Four of them had no
message until 2026-08-14 — `disabled` being the one a real course hits, every
time an author switches the tutor off.
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
    #: **Produced since 2026-08-13**, by `api/deps.contract_version_guard`, a
    #: router-level dependency on the three service-credential routers (ingest,
    #: invalidation, packs). This comment used to say "NOT PRODUCED... which this
    #: deployment cannot have", which was true only while the version lock was
    #: unbuilt; wiring the lock made it false and the comment did not follow.
    #:
    #: It is the one code with no student-facing message, and that exemption is
    #: narrow and checked: `SERVER_TO_SERVER_ONLY` in `test_error_contract.py`
    #: fails if it ever appears on a route a browser can reach. Writing wording
    #: for it would put a string in the UI no student can get to.
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
