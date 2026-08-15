"""The wire-contract version lock, platform side.

Two properties, and the second matters more than the first:

1. The client learns the service's contract version on **first contact**, caches
   it, and raises `ContractMismatch` on skew.
2. **Nothing about this can take the LMS down.** `settings/common.py` says it in
   its own docstring: a plugin that raises during settings loading takes the
   platform down for every course, including the ones that never enabled
   CourseMate. So the check lives at first *use*, where the cost of a mismatch is
   a failed CourseMate call, not a failed boot.

`version.py` used to claim "Both sides assert this at startup". Nothing asserted
anything, and the startup half could not have been written safely.
"""

from __future__ import annotations

import sys
import types

import pytest

# The client module imports `django.conf.settings` lazily, inside each function,
# so it is importable without a configured Django — but it needs `httpx`.
#
# **A missing `httpx` FAILS collection; it does not skip (2026-08-15).**
#
# This was `httpx = pytest.importorskip("httpx")`. The 10 tests below cover the
# contract version lock — the check that refuses a peer speaking a different
# wire format — and without httpx they did not fail, they VANISHED, leaving a
# green run for an unexercised control. `httpx` is declared in
# requirements-dev.txt and is what the client actually calls, so there is no
# environment where skipping is the kind answer rather than the misleading one.
#
# Third of three fixes for this shape: `make test-js` and the pypdf suite were
# the others. The `django` skip in conftest.py is deliberately NOT one of them —
# it guards a fixture rather than a whole file, and what that suite should do
# without Django is a design question, not a defect.
try:
    import httpx
except ImportError as exc:  # pragma: no cover - the message is the point
    raise RuntimeError(
        "httpx is not installed, so the 10 contract-version-lock tests cannot "
        "run. They used to be skipped silently, which reported a green run for "
        "an unexercised control. Install it with "
        "`pip install -r requirements-dev.txt`."
    ) from exc

from coursemate_contracts import CONTRACT_VERSION, ContractMismatch
from coursemate_platform.client import http as client


class _Settings:
    COURSEMATE_SERVICE_URL = "http://service:8000"
    COURSEMATE_HTTP_TIMEOUT_SECONDS = 5
    COURSEMATE_SERVICE_CREDENTIAL = "test-credential"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """A clean cache and a stub `django.conf` for every test."""
    client.reset_peer_contract_for_tests()
    fake_conf = types.ModuleType("django.conf")
    fake_conf.settings = _Settings()
    monkeypatch.setitem(sys.modules, "django.conf", fake_conf)
    yield
    client.reset_peer_contract_for_tests()


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def _health(version):
    return _Response({"status": "ok", "contract_version": version})


# --- matching versions ------------------------------------------------------


def test_a_matching_version_is_accepted_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(client.httpx, "get",
                        lambda url, **kw: (calls.append(url), _health(CONTRACT_VERSION))[1])

    client._assert_peer_contract()
    client._assert_peer_contract()
    client._assert_peer_contract()

    assert len(calls) == 1, "the peer version was re-fetched after being cached"
    assert calls[0].endswith(client.HEALTH_PATH)


def test_the_cached_version_is_the_one_the_service_reported(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _health(CONTRACT_VERSION))
    client._assert_peer_contract()
    assert client._peer_contract_version == CONTRACT_VERSION


# --- mismatched versions ----------------------------------------------------


def test_a_mismatched_version_raises_contract_mismatch(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _health(CONTRACT_VERSION + 1))

    with pytest.raises(ContractMismatch) as exc:
        client._assert_peer_contract()

    assert "coursemate-service" in str(exc.value)
    assert "Deploy both packages together" in str(exc.value)


def test_a_mismatch_is_not_cached_as_success(monkeypatch):
    """A failed check must not leave the client believing it passed."""
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _health(CONTRACT_VERSION + 1))
    with pytest.raises(ContractMismatch):
        client._assert_peer_contract()
    assert client._peer_contract_version is None


def test_a_mismatch_reaches_the_caller_through_post(monkeypatch):
    """The check runs before the request, so a skewed peer is never sent data."""
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _health(CONTRACT_VERSION + 1))

    posted = []
    monkeypatch.setattr(client.httpx, "post", lambda *a, **kw: posted.append(a))

    with pytest.raises(ContractMismatch):
        client.post("/coursemate/api/ingest/blocks", {"any": "payload"})

    assert posted == [], "data was sent to a peer speaking a different contract"


# --- an outage is not a mismatch --------------------------------------------


def test_an_unreachable_service_is_not_treated_as_incompatible(monkeypatch):
    """An outage must not be remembered as an incompatibility that survives it.
    The request that follows fails on its own and the breaker counts it."""
    def boom(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(client.httpx, "get", boom)

    client._assert_peer_contract()          # must not raise
    assert client._peer_contract_version is None


def test_a_health_response_without_a_version_is_tolerated(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _Response({"status": "ok"}))
    client._assert_peer_contract()
    assert client._peer_contract_version is None


# --- the header this side sends ---------------------------------------------


def test_every_call_carries_the_contract_header(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _health(CONTRACT_VERSION))

    sent = {}
    def fake_post(url, **kw):
        sent.update(kw.get("headers") or {})
        return _Response({"ok": True})

    monkeypatch.setattr(client.httpx, "post", fake_post)
    client.post("/coursemate/api/invalidate", {"reason": "test"})

    assert sent[client.CONTRACT_VERSION_HEADER] == str(CONTRACT_VERSION)
    assert sent["Authorization"].startswith("Bearer ")


# --- the guarantee that matters ---------------------------------------------


def test_importing_the_plugin_settings_never_raises():
    """The LMS startup path must stay non-raising. Nothing in the version lock
    may be reachable from settings loading — a plugin that raises there takes
    down every course on the instance, including those that never enabled
    CourseMate (Principle 8)."""
    import pathlib

    common = pathlib.Path(
        "packages/coursemate-platform/coursemate_platform/settings/common.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("assert_compatible", "_assert_peer_contract", "ContractMismatch"):
        assert forbidden not in common, f"{forbidden} reachable from settings loading"


def test_the_plugin_appconfig_does_not_check_versions():
    """Same rule one level up: `AppConfig.ready()` runs during Django startup."""
    import pathlib

    apps = pathlib.Path(
        "packages/coursemate-platform/coursemate_platform/apps.py"
    ).read_text(encoding="utf-8")

    assert "assert_compatible" not in apps
    assert "_assert_peer_contract" not in apps
    assert "def ready" not in apps, (
        "a ready() hook appeared — if it ever calls the service, the LMS boots "
        "only when CourseMate is up"
    )
