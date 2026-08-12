"""Liveness and readiness say different things, and readiness can now fail.

`/health/ready` returned `{"status": "ready"}` unconditionally until 2026-08-13.
A readiness probe that cannot fail is a liveness check with a misleading name —
it would have reported ready throughout the D2 verification, while the service
was running and unable to answer anything.

The line drawn here is the one that already exists elsewhere in the system:

* an index that cannot be OPENED is a fault (no answer can be retrieved),
* an index that is merely EMPTY is not — that is `preparing` (§5.1),
* and Redis being down is a documented degraded mode, not a reason to refuse
  traffic (`shared_state`), so it is reported and never gates.
"""

from __future__ import annotations

import pytest
from coursemate_contracts import CONTRACT_VERSION
from coursemate_service import main
from fastapi.testclient import TestClient

client = TestClient(main.app)

READY = "/coursemate/health/ready"
HEALTH = "/coursemate/health"


class _Store:
    """Stand-in index store."""

    def __init__(self, offerings=(), fail=False):
        self._offerings = list(offerings)
        self._fail = fail

    def indexed_offerings(self):
        if self._fail:
            raise RuntimeError("unable to open database file")
        return self._offerings


class _Redis:
    def __init__(self, fail=False):
        self._fail = fail

    def ping(self):
        if self._fail:
            raise ConnectionError("down")
        return True


@pytest.fixture(autouse=True)
def _healthy(monkeypatch):
    """Default: index openable, Redis up. Each test degrades one thing."""
    monkeypatch.setattr(main, "get_store", lambda: _Store(["course-v1:X+Y+Z"]))
    monkeypatch.setattr(main.shared_state, "get_redis", lambda: _Redis())


# --- ready ------------------------------------------------------------------


def test_ready_when_the_index_opens():
    r = client.get(READY)
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["checks"]["index"] == "ok"


def test_an_EMPTY_index_is_still_ready(monkeypatch):
    """The distinction the old docstring was right about: nothing indexed yet is
    `preparing` to a student, not a broken instance. Refusing traffic here would
    keep a fresh install permanently out of rotation."""
    monkeypatch.setattr(main, "get_store", lambda: _Store([]))

    r = client.get(READY)
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


# --- not ready --------------------------------------------------------------


def test_an_unopenable_index_is_not_ready(monkeypatch):
    monkeypatch.setattr(main, "get_store", lambda: _Store(fail=True))

    r = client.get(READY)
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["checks"]["index"] == "unavailable"


def test_not_ready_is_a_503_not_a_200_with_a_sad_body(monkeypatch):
    """An orchestrator acts on the status code. A 200 saying "not_ready" keeps a
    broken replica in rotation."""
    monkeypatch.setattr(main, "get_store", lambda: _Store(fail=True))
    assert client.get(READY).status_code == 503


def test_the_readiness_failure_is_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(main, "get_store", lambda: _Store(fail=True))
    with caplog.at_level(logging.ERROR, logger=main.log.name):
        client.get(READY)

    assert any("index store unavailable" in r.getMessage() for r in caplog.records)


# --- redis is reported, never gating ----------------------------------------


def test_redis_down_is_reported_but_still_ready(monkeypatch):
    """`shared_state` documents unreachable Redis as a supported degraded mode:
    the limiter and the authz cache fall back to per-process state. Gating on it
    would turn a documented degradation into an outage."""
    monkeypatch.setattr(main.shared_state, "get_redis", lambda: _Redis(fail=True))

    r = client.get(READY)
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["checks"]["redis"] == "degraded"


def test_no_redis_configured_is_reported_but_still_ready(monkeypatch):
    monkeypatch.setattr(main.shared_state, "get_redis", lambda: None)

    r = client.get(READY)
    assert r.status_code == 200
    assert r.json()["checks"]["redis"] == "degraded"


def test_redis_being_up_does_not_rescue_a_broken_index(monkeypatch):
    monkeypatch.setattr(main, "get_store", lambda: _Store(fail=True))
    monkeypatch.setattr(main.shared_state, "get_redis", lambda: _Redis())

    assert client.get(READY).status_code == 503


# --- liveness is unchanged --------------------------------------------------


def test_health_is_still_liveness_and_still_answers_when_not_ready(monkeypatch):
    """Liveness says "this process is alive"; readiness says "it can serve".
    Collapsing them would make an orchestrator kill a pod that only needed its
    index mounted."""
    monkeypatch.setattr(main, "get_store", lambda: _Store(fail=True))

    r = client.get(HEALTH)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["contract_version"] == CONTRACT_VERSION


def test_health_does_not_touch_the_index(monkeypatch):
    """It must stay cheap enough to poll: liveness runs far more often than
    readiness and has no reason to hit the database."""
    touched = []

    class _Watched(_Store):
        def indexed_offerings(self):
            touched.append(1)
            return []

    monkeypatch.setattr(main, "get_store", lambda: _Watched())
    client.get(HEALTH)
    assert touched == []
