"""Rate limiting and the authz cache, on both paths.

These matter because both failures are SILENT. A per-process rate limiter with
N replicas allows N times the limit and raises nothing; a per-process authz
cache means an unenrollment notice returns 200 while other replicas keep serving
the revoked student. Neither shows up as an error, only as "the control does not
work", which is why they need tests that assert the shared path specifically.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from coursemate_service import shared_state
from coursemate_service.api.deps import _RateLimiter
from coursemate_service.boundary.authz import Entitlement, EnrollmentVerifier
from coursemate_service.config import settings


class FakeRedis:
    """Enough Redis for these tests, including pipeline ordering."""

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}
        self.fail = False

    # --- sorted sets ---
    def pipeline(self):
        return _FakePipe(self)

    # --- strings ---
    def get(self, k):
        if self.fail:
            raise ConnectionError("down")
        return self.kv.get(k)

    def setex(self, k, ttl, v):
        if self.fail:
            raise ConnectionError("down")
        self.kv[k] = v

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
        return n

    def scan_iter(self, match=None, count=None):
        import fnmatch
        return [k for k in list(self.kv) if fnmatch.fnmatch(k, match or "*")]


class _FakePipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r, self.ops = r, []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("trim", key, lo, hi)); return self

    def zcard(self, key):
        self.ops.append(("card", key)); return self

    def zadd(self, key, mapping):
        self.ops.append(("add", key, mapping)); return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl)); return self

    def execute(self):
        if self.r.fail:
            raise ConnectionError("down")
        out = []
        for op in self.ops:
            if op[0] == "trim":
                _, key, lo, hi = op
                d = self.r.z.setdefault(key, {})
                for m in [m for m, sc in d.items() if lo <= sc <= hi]:
                    d.pop(m)
                out.append(0)
            elif op[0] == "card":
                out.append(len(self.r.z.setdefault(op[1], {})))
            elif op[0] == "add":
                self.r.z.setdefault(op[1], {}).update(op[2]); out.append(1)
            else:
                out.append(True)
        return out


@pytest.fixture
def fake(monkeypatch):
    """One patch point. Both consumers call `shared_state.get_redis()` through
    the module rather than binding it at import, so patching here reaches them."""
    r = FakeRedis()
    monkeypatch.setattr(shared_state, "get_redis", lambda: r)
    return r


# --- rate limiting ---------------------------------------------------------

def test_shared_limiter_counts_across_replicas(fake, monkeypatch):
    """The bug this fixes: two replicas each counted to the limit separately, so
    a student got twice the allowance and nothing anywhere reported it."""
    monkeypatch.setattr(settings, "student_requests_per_minute", 3)
    replica_a, replica_b = _RateLimiter(), _RateLimiter()

    replica_a.check("s1")
    replica_b.check("s1")
    replica_a.check("s1")
    with pytest.raises(HTTPException) as exc:
        replica_b.check("s1")          # 4th overall, on a DIFFERENT process
    assert exc.value.status_code == 429


def test_limiter_fails_open_when_redis_dies(fake, monkeypatch):
    """Abuse control, not authorization. Denying every student because a cache
    is down trades a small abuse risk for a total outage."""
    monkeypatch.setattr(settings, "student_requests_per_minute", 5)
    fake.fail = True
    _RateLimiter().check("s1")          # must not raise


def test_local_limiter_still_enforces_without_redis(monkeypatch):
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    monkeypatch.setattr(settings, "student_requests_per_minute", 2)
    rl = _RateLimiter()
    rl.check("s1"); rl.check("s1")
    with pytest.raises(HTTPException):
        rl.check("s1")


def test_local_limiter_does_not_grow_without_bound(monkeypatch):
    """It kept one key per student ever seen, for the life of the process."""
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    monkeypatch.setattr(settings, "student_requests_per_minute", 1000)
    rl = _RateLimiter()
    for i in range(1200):
        rl.check(f"student-{i}")
    for k in rl._hits:
        rl._hits[k] = []   # age them out
    rl.check("trigger-prune")
    assert len(rl._hits) < 1200


# --- authz cache -----------------------------------------------------------

def test_entitlement_is_shared_between_replicas(fake, monkeypatch):
    a, b = EnrollmentVerifier(), EnrollmentVerifier()
    calls = []

    def ask(self, user_id, offering_id):
        calls.append(user_id)
        return Entitlement(enrolled=True, is_staff=False, checked_at=time.time())

    monkeypatch.setattr(EnrollmentVerifier, "_ask_platform", ask)
    a.verify("u1", "c1")
    b.verify("u1", "c1")                 # different process, same entitlement
    assert len(calls) == 1, "second replica re-asked the platform"


def test_invalidation_reaches_every_replica(fake, monkeypatch):
    """The security half. `invalidate()` exists so a revoked student stops
    working IMMEDIATELY; with a per-process cache it cleared one dict and the
    other replicas kept serving until the TTL expired."""
    a, b = EnrollmentVerifier(), EnrollmentVerifier()
    monkeypatch.setattr(
        EnrollmentVerifier, "_ask_platform",
        lambda self, u, o: Entitlement(True, False, time.time()),
    )
    a.verify("u1", "c1")
    assert b._cache_get("u1", "c1") is not None

    a.invalidate(user_id="u1", offering_id="c1")
    assert b._cache_get("u1", "c1") is None


def test_cache_read_failure_is_a_miss_not_a_grant(fake, monkeypatch):
    """A corrupt or unreachable cache must never be read as an entitlement."""
    v = EnrollmentVerifier()
    fake.kv["cm:authz:u1:c1"] = "{not json"
    assert v._cache_get("u1", "c1") is None
    fake.fail = True
    assert v._cache_get("u1", "c1") is None
