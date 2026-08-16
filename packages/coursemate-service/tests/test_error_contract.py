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
TUTOR_BLOCK = (
    ROOT / "packages" / "coursemate-platform" / "coursemate_platform"
    / "xblock" / "tutor_block.py"
)

#: Declared, deliberately not produced yet, and therefore not required to have a
#: student-facing message either.
#:
#: **Empty as of 2026-08-13, and that is the mechanism working twice.**
#: ``BUDGET_EXCEEDED`` left in Phase C1 and ``CONTRACT_MISMATCH`` left when the
#: version lock was wired: each time, adding a producer turned
#: `test_the_allowlist_has_not_rotted` red, and clearing that failure forced the
#: rest of the wiring in the same change. The list only ever shrinks.
INTENTIONALLY_UNPRODUCED: frozenset[ErrorCode] = frozenset()

#: Produced, but on routes a BROWSER never calls — so they are exempt from
#: needing a student-facing message, and only from that.
#:
#: ``CONTRACT_MISMATCH`` is raised by `api/deps.contract_version_guard`, a
#: router-level dependency on the ingest and invalidation routers. Those carry
#: the *service* credential and are called by the platform's Celery tasks, never
#: by a student's page. Writing a notice for it would put a string in the UI that
#: no student can reach — the exact dead-wiring this file exists to prevent.
#:
#: The exemption is narrow and is itself checked: `test_a_server_to_server_code
#: _is_never_produced_on_a_student_path` fails if such a code appears anywhere
#: the browser can reach, so this cannot become a way to skip writing a message
#: for a code students actually see.
SERVER_TO_SERVER_ONLY: frozenset[ErrorCode] = frozenset({ErrorCode.CONTRACT_MISMATCH})

PRODUCIBLE = [c for c in ErrorCode if c not in INTENTIONALLY_UNPRODUCED]

#: Producible AND reachable by a browser. This is the set that must have wording.
STUDENT_FACING = [c for c in PRODUCIBLE if c not in SERVER_TO_SERVER_ONLY]


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


@pytest.mark.parametrize("code", STUDENT_FACING, ids=lambda c: c.value)
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


def test_a_server_to_server_code_is_never_produced_on_a_student_path():
    """The exemption above must not become a way to skip writing a message.

    A code is exempt from needing UI wording only because no browser can reach
    the route that raises it. That claim is checked here rather than trusted: if
    `CONTRACT_MISMATCH` ever appears in the chat, exam-prep or pipeline modules —
    anything a student's page calls — the exemption is wrong and this fails.
    """
    student_reachable = [
        SERVICE_SRC / "api" / "chat.py",
        SERVICE_SRC / "api" / "examprep.py",
        SERVICE_SRC / "api" / "plan.py",
        SERVICE_SRC / "api" / "packs.py",
        *(SERVICE_SRC / "ai").glob("*.py"),
        *(SERVICE_SRC / "agents").glob("*.py"),
    ]
    for code in SERVER_TO_SERVER_ONLY:
        for path in student_reachable:
            if not path.exists():
                continue
            assert f"ErrorCode.{code.name}" not in path.read_text(encoding="utf-8"), (
                f"{code.value} is exempt from needing UI wording because a browser "
                f"cannot reach it — but it is produced in {path.name}"
            )


def test_the_unproduced_allowlist_is_empty_and_should_stay_that_way():
    """Not a style preference. An entry here is a declared code that nothing
    raises: a promise the type makes that no code path keeps. It reached empty
    on 2026-08-13 and every future entry should be temporary."""
    assert INTENTIONALLY_UNPRODUCED == frozenset()


# --- end three: the XBLOCK's own error vocabulary ---------------------------
#
# **The blind spot this file had until 2026-08-14.** Everything above checks the
# `ErrorCode` enum against `NOTICES`, and both ends were correctly wired. The
# XBlock's JSON handlers have a SEPARATE vocabulary — plain strings in
# `return {"error": "..."}` — which reaches the exact same `showNotice(code)`
# lookup and was in neither set. Four of them had no message:
#
#     disabled, forbidden, bad_request, invalid_mode
#
# `disabled` is returned by `mint()` whenever an author unchecks "enabled" in
# Studio. So switching the tutor off — a normal, deliberate act — told every
# student in the course "Something went wrong."
#
# This is the same defect the file was written for, one layer up, and the reason
# it survived is instructive: the check was bound to the enum rather than to the
# thing it cared about, which is *every string that can reach showNotice*.

#: Error strings the JS never routes through `showNotice`, with the reason.
#: These are validation messages for the mastery handler, rendered inline beside
#: the control that produced them rather than in the notice bar.
NOT_ROUTED_TO_NOTICES: frozenset[str] = frozenset({
    "clo_id, question_id and attempt_id are all required",
    "difficulty_band must be easy, medium or hard",
    "invalid identifier",
    # Same category: a mastery-handler validation message. It is also
    # unreachable from the shipped client, which never sends `source` — it fires
    # only if something starts claiming an attempt was graded, and the only
    # audience for that sentence is whoever wrote that caller.
    "source must be self_reported; nothing here evaluates answers",
    # Conversation switching. Rendered by the picker putting itself back where
    # it was, not in the notice bar: the student's chat is unchanged, so a
    # page-level alarm would overstate what happened. Only reachable from a
    # client asking for a conversation that does not exist, which is a bug in
    # the caller rather than a state the student can reach.
    "no such conversation",
})


def xblock_error_strings() -> set[str]:
    """Every literal in `return {"error": "..."}` in the XBlock's handlers."""
    src = TUTOR_BLOCK.read_text(encoding="utf-8")
    return set(re.findall(r'return \{"error":\s*"([^"]+)"', src))


def test_the_scan_finds_the_xblock_error_strings_at_all():
    """A regex that matches nothing would make every test below vacuous — the
    exact failure mode this file exists to catch, so it is checked rather than
    assumed."""
    found = xblock_error_strings()
    assert len(found) >= 6, f"the scan found only {found}; the pattern has rotted"
    assert "disabled" in found


@pytest.mark.parametrize(
    "code", sorted(xblock_error_strings() - NOT_ROUTED_TO_NOTICES)
)
def test_every_xblock_error_string_has_a_student_facing_message(code):
    assert code in ui_notice_keys(), (
        f"tutor_block.py returns {{'error': '{code}'}}, tutor.js passes it "
        f"straight to showNotice(), and NOTICES has no entry — so the student "
        f"sees the generic fallback for a state the block named. Add wording, "
        f"or add it to NOT_ROUTED_TO_NOTICES with a reason."
    )


def test_the_exemption_list_has_not_rotted():
    """An exemption for a string the block no longer returns is an exemption
    that reads as if it still covers something."""
    stale = NOT_ROUTED_TO_NOTICES - xblock_error_strings()
    assert not stale, f"these are exempted but no longer returned: {sorted(stale)}"


def test_disabled_says_switched_off_rather_than_broken():
    """The specific regression, pinned by meaning and not only by presence.

    An author unchecking "enabled" is not a fault, and the message must not read
    like one — that is the same distinction NOT_A_FAULT draws for ABSTAINED and
    PREPARING."""
    message = re.search(
        r'disabled: "([^"]+)"', TUTOR_JS.read_text(encoding="utf-8")
    ).group(1)
    assert re.search(r"off|unavailable for|not enabled", message, re.IGNORECASE), message
    assert "went wrong" not in message.lower()
