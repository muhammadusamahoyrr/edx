"""The wire-contract version lock, service side.

`assert_compatible` existed from the first commit, its docstring said "Both sides
assert this at startup", and **nothing called it**. `contract_version_lock` was
read by nothing and `CONTRACT_MISMATCH` was produced by nothing — a four-part
feature that a reader would believe was active. These tests are what make the
guarantee real on this side.

The check is a router-level dependency on the two server-to-server routers, not
on student traffic: a browser has no contract version, and the student path is
already scoped by a signed short-lived token.
"""

from __future__ import annotations

import pytest
from coursemate_contracts import CONTRACT_VERSION
from coursemate_contracts.errors import ErrorCode
from coursemate_service.api import deps
from fastapi import HTTPException

HEADER = deps.CONTRACT_VERSION_HEADER


@pytest.fixture(autouse=True)
def _lock_on(monkeypatch):
    monkeypatch.setattr(deps.settings, "contract_version_lock", True)
    deps.reset_contract_warning_for_tests()
    yield
    deps.reset_contract_warning_for_tests()


def guard(value):
    return deps.contract_version_guard(x_coursemate_contract_version=value)


# --- the lock doing its job -------------------------------------------------


def test_a_matching_version_is_allowed():
    assert guard(str(CONTRACT_VERSION)) is None


def test_a_mismatched_version_is_refused_with_contract_mismatch():
    with pytest.raises(HTTPException) as exc:
        guard(str(CONTRACT_VERSION + 1))

    assert exc.value.status_code == 409
    assert exc.value.detail == ErrorCode.CONTRACT_MISMATCH.value


def test_an_older_peer_is_refused_too():
    """Skew in either direction is skew."""
    with pytest.raises(HTTPException) as exc:
        guard(str(CONTRACT_VERSION - 1))
    assert exc.value.detail == ErrorCode.CONTRACT_MISMATCH.value


def test_an_unparseable_version_is_a_mismatch_not_an_unknown():
    """Absent means "too old to announce". A header that IS sent and cannot be
    read means something is speaking this protocol and getting it wrong, which is
    a real disagreement about the wire."""
    for junk in ("v1", "", "1.0", "abc"):
        with pytest.raises(HTTPException) as exc:
            guard(junk)
        assert exc.value.detail == ErrorCode.CONTRACT_MISMATCH.value, junk


# --- absence is tolerated, on purpose ---------------------------------------


def test_a_missing_header_is_allowed():
    """A platform old enough not to send the header cannot be told apart from
    one that failed to. Refusing it would make this check break the very upgrade
    it exists to make safe — whichever side deployed first would reject the
    other."""
    assert guard(None) is None


def test_the_missing_header_warning_is_logged_once(caplog):
    """A per-request line during a rollout buries the thing that matters."""
    import logging

    with caplog.at_level(logging.WARNING, logger=deps.log.name):
        guard(None)
        guard(None)
        guard(None)

    warnings = [r for r in caplog.records if HEADER in r.getMessage()]
    assert len(warnings) == 1


# --- the kill switch --------------------------------------------------------


def test_the_lock_disabled_checks_nothing(monkeypatch):
    """With the lock off nothing is checked — that is the switch working, not
    the check being broken."""
    monkeypatch.setattr(deps.settings, "contract_version_lock", False)

    assert guard(str(CONTRACT_VERSION + 99)) is None
    assert guard("nonsense") is None
    assert guard(None) is None


def test_the_lock_is_read_at_call_time_not_import_time(monkeypatch):
    """Otherwise flipping the switch would need a restart, and a kill switch
    that needs a deploy is not one."""
    monkeypatch.setattr(deps.settings, "contract_version_lock", False)
    assert guard(str(CONTRACT_VERSION + 1)) is None

    monkeypatch.setattr(deps.settings, "contract_version_lock", True)
    with pytest.raises(HTTPException):
        guard(str(CONTRACT_VERSION + 1))


# --- where it is applied ----------------------------------------------------


def test_both_server_to_server_routers_carry_the_guard():
    """Router-level, so a NEW route on either inherits it. Checked structurally
    because the failure mode is a route added later without it."""
    from coursemate_service.api import ingest, invalidation

    for module in (ingest, invalidation):
        deps_on_router = [
            d.dependency for d in module.router.dependencies  # type: ignore[attr-defined]
        ]
        assert deps.contract_version_guard in deps_on_router, module.__name__
        assert deps.service_credential in deps_on_router, module.__name__


def test_student_traffic_does_not_carry_the_guard():
    """A browser has no contract version. Applying it there would refuse every
    student the moment the header were required."""
    from coursemate_service.api import chat, examprep

    for module in (chat, examprep):
        deps_on_router = [d.dependency for d in module.router.dependencies]
        assert deps.contract_version_guard not in deps_on_router, module.__name__


def test_the_header_name_matches_the_one_the_platform_sends():
    """The two packages cannot import each other (.importlinter contract 2), so
    the header name is spelled twice. This is what keeps the two spellings from
    drifting into a check that silently never fires."""
    import pathlib

    client = pathlib.Path(
        "packages/coursemate-platform/coursemate_platform/client/http.py"
    ).read_text(encoding="utf-8")
    assert f'CONTRACT_VERSION_HEADER = "{HEADER}"' in client
