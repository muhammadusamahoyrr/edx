"""Authorization re-derivation (§10.1).

The property under test is not "a valid token is accepted" — that was already
true. It is that **a valid token is not sufficient**: the platform is asked, on
every call, whether the enrollment still holds.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_service.boundary.authz import (
    Entitlement,
    EnrollmentVerifier,
    NotEnrolled,
    PlatformUnreachable,
)


def _claims(offering="CS101", sub="u1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(sub=sub, course_id=offering, offering_id=offering,
                         roles=["student"], aud=AUDIENCE_STUDENT,
                         exp=now + 300, iat=now, usage_key="u", block_id="b")


class _FakePlatform:
    """Records calls so we can assert on caching as well as outcomes."""

    def __init__(self, enrolled: bool = True, fail: bool = False):
        self.enrolled, self.fail, self.calls = enrolled, fail, 0

    def __call__(self, user_id, offering_id):
        self.calls += 1
        if self.fail:
            raise PlatformUnreachable("LMS down")
        return Entitlement(enrolled=self.enrolled, is_staff=False, checked_at=time.time())


def test_enrolled_user_is_allowed():
    v = EnrollmentVerifier()
    v._ask_platform = _FakePlatform(enrolled=True)
    assert v.require_enrolled("u1", "CS101").enrolled


def test_valid_token_is_not_enough_when_platform_says_unenrolled():
    """The core of this phase.

    The token is signed, unexpired and correctly scoped — and access is still
    denied, because the platform is the authority on enrollment and it said no.
    """
    v = EnrollmentVerifier()
    v._ask_platform = _FakePlatform(enrolled=False)
    with pytest.raises(NotEnrolled):
        v.require_enrolled("u1", "CS101")


def test_unreachable_platform_fails_closed():
    """Availability must not become an authorization bypass."""
    v = EnrollmentVerifier()
    v._ask_platform = _FakePlatform(fail=True)
    with pytest.raises(PlatformUnreachable):
        v.require_enrolled("u1", "CS101")


def test_result_is_cached_so_the_common_case_is_free():
    v = EnrollmentVerifier()
    fake = _FakePlatform(enrolled=True)
    v._ask_platform = fake
    for _ in range(5):
        v.require_enrolled("u1", "CS101")
    assert fake.calls == 1, f"expected 1 platform call, made {fake.calls}"


def test_cache_expires_so_revocation_takes_effect(monkeypatch):
    """§6.4 makes this the shortest-lived tier precisely because a revoked
    enrollment must stop working quickly."""
    from coursemate_service.boundary import authz

    monkeypatch.setattr(authz.settings, "authz_cache_ttl_seconds", 0)
    v = EnrollmentVerifier()
    fake = _FakePlatform(enrolled=True)
    v._ask_platform = fake
    v.require_enrolled("u1", "CS101")
    v.require_enrolled("u1", "CS101")
    assert fake.calls == 2, "expired entry was not re-checked"


def test_invalidate_makes_revocation_immediate():
    """The LMS unenrollment receiver calls this, so revocation does not wait out
    the TTL (§3.4 rule 4)."""
    v = EnrollmentVerifier()
    fake = _FakePlatform(enrolled=True)
    v._ask_platform = fake
    v.require_enrolled("u1", "CS101")
    assert v.invalidate(user_id="u1") == 1
    v.require_enrolled("u1", "CS101")
    assert fake.calls == 2


def test_cache_is_keyed_per_user_and_offering():
    """A cache that collided across users would be an authorization bug, not a
    performance one."""
    v = EnrollmentVerifier()
    fake = _FakePlatform(enrolled=True)
    v._ask_platform = fake
    v.require_enrolled("u1", "CS101")
    v.require_enrolled("u2", "CS101")
    v.require_enrolled("u1", "BIO200")
    assert fake.calls == 3


def test_boundary_denies_when_platform_says_unenrolled(monkeypatch, tmp_path):
    """End to end through the boundary: retrieval is refused, and no content is
    returned in the error path."""
    from coursemate_service.boundary import impl as boundary_impl
    from coursemate_service.knowledge.store import ChunkStore

    store = ChunkStore(tmp_path / "i.db")
    monkeypatch.setattr(boundary_impl, "get_store", lambda: store)
    monkeypatch.setattr(boundary_impl.settings, "enforce_enrollment", True)
    monkeypatch.setattr(boundary_impl.verifier, "_ask_platform", _FakePlatform(enrolled=False))
    boundary_impl.verifier.invalidate()

    with pytest.raises(boundary_impl.AuthorizationError):
        boundary_impl.CourseIntelligenceImpl().retrieve_course_context(
            "anything", "CS101", _claims("CS101")
        )


def test_boundary_denies_when_platform_unreachable(monkeypatch, tmp_path):
    from coursemate_service.boundary import impl as boundary_impl
    from coursemate_service.knowledge.store import ChunkStore

    store = ChunkStore(tmp_path / "i2.db")
    monkeypatch.setattr(boundary_impl, "get_store", lambda: store)
    monkeypatch.setattr(boundary_impl.settings, "enforce_enrollment", True)
    monkeypatch.setattr(boundary_impl.verifier, "_ask_platform", _FakePlatform(fail=True))
    boundary_impl.verifier.invalidate()

    with pytest.raises(boundary_impl.AuthorizationError):
        boundary_impl.CourseIntelligenceImpl().retrieve_course_context(
            "anything", "CS101", _claims("CS101")
        )
