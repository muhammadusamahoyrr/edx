"""Every declared ErrorCode is produced somewhere, and everything produced is said.

`ErrorCode` is a contract with two ends and nothing was checking either. A code
can be declared and never produced — a promise in the type that no code path
keeps — or produced and never handled by the UI, which is worse: the student gets
a real, specific state rendered as "Something went wrong."

Both had happened. `UNAUTHENTICATED` was produced by five handler paths and had
no notice, so an expired session told the student nothing and invited them to
retry something that could not work. `UNSUPPORTED_CLAIM` was declared and never
produced at all until it was fixed.

**Deliberately a text scan, not an analyzer.** It looks for the exact token
`ErrorCode.NAME` in the service source, and for a key in the `NOTICES` object in
`tutor.js`. An import-graph walk or an AST pass would catch more and would also
be a second implementation of the thing it checks — the failure mode where the
checker and the code are wrong in the same direction. A grep is dumb enough to
be trusted, and its failure mode is a false alarm someone has to look at, which
is the right direction to fail in.

It reads the platform's JS as *data*, by path. That is not an import and does not
cross the boundary `.importlinter` contract 2 defends; the file is read the same
way a fixture is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from coursemate_contracts.errors import NOT_A_FAULT, ErrorCode

ROOT = Path(__file__).resolve().parents[3]
SERVICE_SRC = ROOT / "packages" / "coursemate-service" / "coursemate_service"
TUTOR_JS = (
    ROOT / "packages" / "coursemate-platform" / "coursemate_platform"
    / "xblock" / "static" / "js" / "src" / "tutor.js"
)

#: Declared, deliberately not produced yet, and therefore not required to have a
#: student-facing message either.
#:
#: * ``BUDGET_EXCEEDED`` — there is no spend ceiling to exceed. The cost path
#:   tracks spend but nothing refuses on it. Phase C owns this.
#: * ``CONTRACT_MISMATCH`` — reserved for a service/platform version skew that
#:   the deployment cannot currently produce: both ship in the same repo and the
#:   XBlock pins no version, so there is no skew to detect.
#:
#: **This list is small on purpose and is asserted in both directions.** An entry
#: here is a claim that the code has NO producer, and `test_the_allowlist_has_not
#: _rotted` fails if one appears. So the day Phase C raises `BUDGET_EXCEEDED`,
#: this test goes red and the fix is to delete the line — which then makes the UI
#: test demand a message for it. That ordering is the point: a code cannot become
#: producible without someone being told to write what the student reads.
INTENTIONALLY_UNPRODUCED: frozenset[ErrorCode] = frozenset(
    {ErrorCode.BUDGET_EXCEEDED, ErrorCode.CONTRACT_MISMATCH}
)

PRODUCIBLE = [c for c in ErrorCode if c not in INTENTIONALLY_UNPRODUCED]


def service_source() -> str:
    """Every .py under the service package, concatenated. Tests excluded — a
    code produced only by a test is not produced."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(SERVICE_SRC.rglob("*.py"))
    )


def producers(code: ErrorCode, source: str) -> int:
    return len(re.findall(rf"\bErrorCode\.{code.name}\b", source))


def ui_notice_keys() -> set[str]:
    """The keys of the `NOTICES` object literal in tutor.js.

    Only `NOTICES` — `PREP_NOTICES` overrides wording for the exam-prep tab and
    falls back to `NOTICES`, so a code present only in the override would still
    be unhandled in chat.
    """
    src = TUTOR_JS.read_text(encoding="utf-8")
    block = re.search(r"\bvar NOTICES = \{(.*?)\n  \};", src, re.DOTALL)
    assert block, "could not find the NOTICES object in tutor.js"
    return set(re.findall(r"^\s*(\w+):", block.group(1), re.MULTILINE))


# --- the declaration is intact ---------------------------------------------


def test_the_allowlist_only_names_codes_that_exist():
    """A renamed or deleted enum member must not leave a silent exemption
    behind. Without this, deleting `BUDGET_EXCEEDED` would leave an allowlist
    entry that exempts nothing and reads as if it still does."""
    assert INTENTIONALLY_UNPRODUCED <= set(ErrorCode)


def test_truncated_is_not_an_error_code():
    """`truncated` is a flag on the DONE frame, not a fault: the answer arrived,
    it was cut short. It has a NOTICES entry because the UI must say so, and it
    must stay out of this enum — promoting it would make a delivered answer
    indistinguishable from a failure to deliver one."""
    assert "truncated" not in {c.value for c in ErrorCode}
    assert "truncated" in ui_notice_keys()


# --- end one: declared -> produced -----------------------------------------


@pytest.mark.parametrize("code", PRODUCIBLE, ids=lambda c: c.value)
def test_every_declared_code_has_a_producer(code):
    assert producers(code, service_source()) > 0, (
        f"{code.value} is declared but nothing in the service produces it. "
        f"Either give it a producer or add it to INTENTIONALLY_UNPRODUCED with "
        f"a reason."
    )


def test_the_allowlist_has_not_rotted():
    """The other direction, and the reason the allowlist is safe to have.

    An exemption that outlives the gap it covers is how this drift returns: the
    code gains a producer, the allowlist still exempts it from needing a UI
    message, and the student sees "Something went wrong" for a state the service
    deliberately reported."""
    src = service_source()
    for code in INTENTIONALLY_UNPRODUCED:
        assert producers(code, src) == 0, (
            f"{code.value} now has a producer, so remove it from "
            f"INTENTIONALLY_UNPRODUCED — and give it a message in NOTICES."
        )


# --- end two: produced -> handled in the UI --------------------------------


@pytest.mark.parametrize("code", PRODUCIBLE, ids=lambda c: c.value)
def test_every_producible_code_has_a_student_facing_message(code):
    assert code.value in ui_notice_keys(), (
        f"{code.value} can reach the browser and has no entry in NOTICES, so a "
        f"student sees the generic fallback for a state the service named."
    )


def test_unauthenticated_is_handled_and_says_what_to_do():
    """The specific regression. Five handler paths return it and until
    2026-08-12 every one of them rendered the generic fallback."""
    assert "unauthenticated" in ui_notice_keys()
    src = TUTOR_JS.read_text(encoding="utf-8")
    message = re.search(r"unauthenticated: \"([^\"]+)\"", src).group(1)
    # It must tell the student to re-authenticate. Retrying is the one thing
    # that cannot work, and it is what the generic message invites.
    assert re.search(r"sign in|log in|expired", message, re.IGNORECASE), message


def test_the_states_that_are_not_faults_still_have_wording():
    """ABSTAINED and PREPARING are correct behaviour. They need messages MORE
    than the faults do — they are the two the design leans on to look honest
    rather than broken."""
    for code in NOT_A_FAULT:
        assert code.value in ui_notice_keys()
