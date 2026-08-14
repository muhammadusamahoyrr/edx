"""Controls that reported success while doing nothing.

Seven fixes from the 2026-08-14 audit, grouped because they share one shape and
one lesson: each was a guard whose *presence* was checked by review and whose
*effect* was checked by nobody. Three had comments describing behaviour they did
not have.

    the rate limiter's prune       removed nothing, ever
    the authz cache               had no eviction at all
    Redis resolution              gave up permanently on one failed connection
    abstentions_total             missed every cache-replayed abstention
    provider_failures_total       could not see a silently degrading primary
    the contract version lock     covered two of the three service routers
    metrics.increment             raised KeyError inside a "never raises" path

Every test here measures the thing rather than reading the code that claims it.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from coursemate_service import metrics, shared_state
from coursemate_service.api import deps
from coursemate_service.boundary.authz import Entitlement, EnrollmentVerifier
from coursemate_service.config import settings

SERVICE_SRC = Path(__file__).resolve().parents[1] / "coursemate_service"


# --- the rate limiter's prune ---------------------------------------------


def test_the_local_limiter_actually_drops_quiet_students():
    """The measurement that exposed it.

    The old guard was `if not v` — true only for an empty window, and a window
    is only re-filtered on that student's own next request, so it was never
    empty for anyone who had gone away. 3,000 stale entries survived 50 prune
    passes; the fiftieth found the same 3,001 keys as the first.
    """
    limiter = deps._RateLimiter()
    now = time.time()

    for i in range(3000):
        limiter._check_local(f"student-{i}", now - 600)
    assert len(limiter._hits) == 3000, "setup did not build the leak"

    limiter._check_local("active", now)

    assert len(limiter._hits) == 1, (
        f"{len(limiter._hits)} entries survived the prune; it removes nothing"
    )
    assert "active" in limiter._hits, "the prune dropped a student mid-window"


def test_the_prune_keeps_everyone_still_inside_the_window():
    """The other direction. A prune that drops live windows would let a student
    exceed the limit by going quiet for a few seconds."""
    limiter = deps._RateLimiter()
    now = time.time()

    for i in range(1200):
        limiter._check_local(f"student-{i}", now)

    limiter._check_local("student-0", now)
    assert len(limiter._hits) == 1200, "a window inside the sliding window was dropped"


def test_the_prune_stays_out_of_the_way_below_the_threshold():
    limiter = deps._RateLimiter()
    now = time.time()
    for i in range(10):
        limiter._check_local(f"student-{i}", now - 600)
    limiter._check_local("active", now)
    assert len(limiter._hits) == 11, "pruning below 1000 keys is wasted work"


# --- the authz cache had no eviction at all -------------------------------


def test_the_authz_cache_evicts_entries_that_outlived_their_ttl(monkeypatch):
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    verifier = EnrollmentVerifier()
    stale = time.time() - (settings.authz_cache_ttl_seconds + 60)

    for i in range(1500):
        verifier._cache[(f"student-{i}", "course-v1:X+Y+Z")] = Entitlement(
            enrolled=True, is_staff=False, checked_at=stale
        )

    verifier._cache_put("fresh", "course-v1:X+Y+Z",
                        Entitlement(enrolled=True, is_staff=False, checked_at=time.time()))

    assert len(verifier._cache) == 1, (
        f"{len(verifier._cache)} expired entitlements retained; the dict grows "
        f"one key per (student, offering) the process ever saw"
    )
    assert ("fresh", "course-v1:X+Y+Z") in verifier._cache


def test_a_live_entitlement_is_never_evicted(monkeypatch):
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    verifier = EnrollmentVerifier()
    now = time.time()
    for i in range(1500):
        verifier._cache[(f"student-{i}", "off")] = Entitlement(
            enrolled=True, is_staff=False, checked_at=now
        )
    verifier._cache_put("fresh", "off",
                        Entitlement(enrolled=True, is_staff=False, checked_at=now))
    assert len(verifier._cache) == 1501, "an unexpired entitlement was dropped"


# --- Redis resolution used to give up forever ------------------------------


class _DeadRedis:
    """A client whose ping always fails, as an unreachable server does."""

    def ping(self):
        raise ConnectionError("nope")


def _install_fake_redis(monkeypatch, module):
    monkeypatch.setitem(__import__("sys").modules, "redis", module)


def test_a_failed_resolution_is_retried_rather_than_remembered(monkeypatch):
    """The bug: `_resolved = True` was set BEFORE the connection attempt and
    never cleared, so one unreachable moment pinned the process to per-process
    state for its whole life — taking the rate limiter, the authz cache and its
    invalidation (a security control), and the budget ledger with it."""
    shared_state.reset_for_tests()
    monkeypatch.setattr(shared_state.settings, "redis_url", "redis://x:6379/1")

    attempts = {"n": 0}

    class _FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(*a, **kw):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return _DeadRedis()
                return _LiveRedis()

    class _LiveRedis:
        def ping(self):
            return True

    _install_fake_redis(monkeypatch, _FakeRedisModule)

    assert shared_state.get_redis() is None, "a dead server should resolve to None"
    assert attempts["n"] == 1

    # Inside the cooldown: no new attempt, still None.
    assert shared_state.get_redis() is None
    assert attempts["n"] == 1, "the cooldown did not suppress the retry"

    # Past the cooldown, Redis is back.
    monkeypatch.setattr(shared_state, "_retry_after", time.monotonic() - 1)
    assert shared_state.get_redis() is not None, (
        "Redis came back and the process never noticed — the fix is not working"
    )
    shared_state.reset_for_tests()


def test_a_working_connection_is_still_resolved_only_once(monkeypatch):
    """Only FAILURE is retried. Re-resolving a healthy client every call would
    be churn, and the client handles its own reconnection."""
    shared_state.reset_for_tests()
    monkeypatch.setattr(shared_state.settings, "redis_url", "redis://x:6379/1")
    calls = {"n": 0}

    class _FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(*a, **kw):
                calls["n"] += 1

                class _Live:
                    def ping(self):
                        return True

                return _Live()

    _install_fake_redis(monkeypatch, _FakeRedisModule)

    for _ in range(5):
        assert shared_state.get_redis() is not None
    assert calls["n"] == 1
    shared_state.reset_for_tests()


def test_no_redis_url_is_settled_immediately_and_not_retried(monkeypatch):
    """"Single process" is a configuration, not a failure. Retrying it every 30s
    would log an outage that is not happening."""
    shared_state.reset_for_tests()
    monkeypatch.setattr(shared_state.settings, "redis_url", "")
    assert shared_state.get_redis() is None
    assert shared_state._resolved is True
    shared_state.reset_for_tests()


# --- metrics ----------------------------------------------------------------


def test_a_degraded_answer_has_a_counter_of_its_own():
    """`provider_failures_total` cannot see a silent degradation — the Router
    swallows the failure whenever a fallback succeeds, so a primary degrading on
    every request moved it by 0 (ADR-0001, measured)."""
    assert "degraded_answers_total" in metrics.snapshot()


def test_the_degraded_counter_moves_where_the_degraded_frame_is_raised():
    """Wired, not merely declared. This repository has shipped a counter, a
    setting and a version lock that nothing called."""
    src = (SERVICE_SRC / "ai" / "pipeline.py").read_text(encoding="utf-8")
    assert 'metrics.increment("degraded_answers_total")' in src
    degraded_block = src.split("!= PRIMARY_DEPLOYMENT", 1)[1][:600]
    assert "degraded_answers_total" in degraded_block, (
        "the counter exists but is not incremented beside the DEGRADED frame"
    )


def test_a_replayed_abstention_is_still_counted():
    """Counting only the recomputed ones would make the abstention rate FALL as
    the cache warmed, which reads as the tutor answering more."""
    src = (SERVICE_SRC / "ai" / "pipeline.py").read_text(encoding="utf-8")
    hit_block = src.split("cache_hits_total", 1)[1].split("return", 1)[0]
    assert "abstentions_total" in hit_block


def test_every_counter_name_used_in_the_service_is_declared():
    """`metrics.increment` raises KeyError on an unknown name — deliberately, so
    a typo cannot create a metric nobody reads. But it is called from inside
    `pipeline.stream`, which promises *"Never raises — failures become frames"*,
    so a typo would break a student's answer rather than a dashboard.

    This moves that failure to build time and keeps the strictness."""
    declared = set(metrics.snapshot())
    used: set[str] = set()

    for path in SERVICE_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "increment"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                used.add(node.args[0].value)

    assert used, "no metrics.increment calls found; the scan has rotted"
    assert used <= declared, (
        f"undeclared counter(s) {sorted(used - declared)} — increment() would "
        f"raise KeyError on the streaming path"
    )


# --- the contract version lock covered two of three routers ----------------


def _all_routes(router) -> list:
    """Flatten the route tree.

    `include_router` does not splice routes into `app.routes` on this FastAPI
    version — it appends an `_IncludedRouter`, which exposes neither `.routes`
    nor `.router` and keeps the real one on `.original_router`. A naive walk
    therefore sees only `/health`, `/health/ready` and `/metrics`, finds no
    service-credential route at all, and reports the whole check as passing.

    That is exactly the shape of bug this file is about, so the caller asserts
    it found some routes rather than trusting an empty result.
    """
    found = []
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is None and hasattr(route, "routes"):
            inner = route
        if inner is not None and inner is not router:
            found.extend(_all_routes(inner))
        else:
            found.append(route)
    return found


def _resolved_dependencies(route) -> set:
    """Every callable FastAPI will run for this route, router-level included.

    Read off `route.dependant`, which is what the framework actually resolves —
    router-level and per-route dependencies are already merged there. Reading
    `route.dependencies` instead would see only the per-route half and would
    have reported `packs` as covered once the router gained the guard, which is
    the wrong direction for this test to be wrong in.
    """
    found = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if getattr(node, "call", None) is not None:
            found.add(node.call)
        stack.extend(getattr(node, "dependencies", []))
    return found


def test_the_contract_guard_covers_every_service_credential_router():
    """Derived from the app, not from prose.

    `deps.py` described the guard as covering "the two server-to-server routers".
    There were three: `packs` carries the same credential and was missed. A count
    written in a docstring cannot notice a third router being added — this can.
    """
    import os

    os.environ.setdefault("COURSEMATE_JWT_SIGNING_KEY", "k" * 40)
    os.environ.setdefault("COURSEMATE_SERVICE_CREDENTIAL", "s" * 40)
    from coursemate_service.main import app

    checked, missing = 0, []
    for route in _all_routes(app):
        resolved = _resolved_dependencies(route)
        if deps.service_credential not in resolved:
            continue
        if getattr(route, "path", "").endswith("/metrics"):
            # Operational surface, scraped by Prometheus rather than called by
            # another deployment of our code. It has no wire contract to skew.
            continue
        checked += 1
        if deps.contract_version_guard not in resolved:
            missing.append(route.path)

    assert checked >= 3, f"only {checked} service-credential routes found; the scan has rotted"
    assert not missing, (
        f"these service-credential routes have no contract version guard: "
        f"{sorted(set(missing))}"
    )


@pytest.mark.parametrize("module", ["ingest", "invalidation", "packs"])
def test_each_service_router_declares_the_guard_at_router_level(module):
    """Router-level, so a NEW route inherits it. Per-route decoration is how
    `packs` ended up with the credential and not the lock — the route that
    existed was decorated, and the router it hung on was not."""
    tree = ast.parse((SERVICE_SRC / "api" / f"{module}.py").read_text(encoding="utf-8"))

    call = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "router" for t in node.targets)
        and isinstance(node.value, ast.Call)
    )
    declared = ast.unparse(call)

    assert "contract_version_guard" in declared, (
        f"{module}: the router does not declare the version guard, so a new "
        f"route added to it inherits no wire-contract check"
    )
    assert "service_credential" in declared
