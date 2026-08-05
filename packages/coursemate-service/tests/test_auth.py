"""Token verification at the service boundary.

Since design v8 the browser holds this token, so everything it asserts is
attacker-controlled in the threat model. These tests pin the properties that make
that safe.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from coursemate_contracts.auth import AUDIENCE_SERVICE, AUDIENCE_STUDENT
from fastapi import HTTPException

KEY = "service-signing-key-at-least-32-bytes!"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """A settings object with this file's test keys, restored afterwards.

    **The restore is the point.** This fixture used to do
    `config.settings = config.Settings()` as a plain assignment, which
    monkeypatch cannot undo — so every test that ran after this file saw a
    *different* settings object than the one it had imported. A later test doing
    `monkeypatch.setattr(settings, ...)` patched the original while the code
    under test read the replacement, and the patch appeared to do nothing.

    That failed in the full suite and passed when the file was run alone, which
    is the worst shape a test bug can have: it looks like flakiness and gets
    re-run rather than fixed.
    """
    monkeypatch.setenv("COURSEMATE_JWT_SIGNING_KEY", KEY)
    monkeypatch.setenv("COURSEMATE_SERVICE_CREDENTIAL", "service-credential-32-bytes-minimum!")
    from coursemate_service import config
    import coursemate_service.api.deps as deps

    fresh = config.Settings()  # type: ignore[call-arg]
    monkeypatch.setattr(config, "settings", fresh)
    monkeypatch.setattr(deps, "settings", fresh)
    return fresh


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "u1",
        "course_id": "course-v1:ACME+CS101+2026",
        "offering_id": "CS101-2026-FALL",
        "roles": ["student"],
        "aud": AUDIENCE_STUDENT,
        "iss": "coursemate-xblock",
        "iat": now,
        "exp": now + 300,
        "usage_key": None,
    }
    claims.update(overrides)
    return pyjwt.encode(claims, KEY, algorithm="HS256")


def test_valid_token_is_accepted():
    from coursemate_service.api.deps import student_claims

    claims = student_claims(f"Bearer {_token()}")
    assert claims.sub == "u1"
    assert claims.offering_id == "CS101-2026-FALL"


def test_missing_header_is_rejected():
    from coursemate_service.api.deps import student_claims

    with pytest.raises(HTTPException) as exc:
        student_claims(None)
    assert exc.value.status_code == 401


def test_expired_token_is_rejected():
    from coursemate_service.api.deps import student_claims

    with pytest.raises(HTTPException) as exc:
        student_claims(f"Bearer {_token(exp=int(time.time()) - 10)}")
    assert exc.value.status_code == 401


def test_token_signed_with_another_key_is_rejected():
    from coursemate_service.api.deps import student_claims

    forged = pyjwt.encode({"sub": "attacker", "aud": AUDIENCE_STUDENT}, "wrong-key-but-long-enough-32b", algorithm="HS256")
    with pytest.raises(HTTPException):
        student_claims(f"Bearer {forged}")


def test_service_credential_cannot_be_used_on_the_student_path():
    """A leaked ingest credential must not become a student session, and vice
    versa — that separation is the whole reason there are two (§3.4)."""
    from coursemate_service.api.deps import student_claims

    with pytest.raises(HTTPException) as exc:
        student_claims(f"Bearer {_token(aud=AUDIENCE_SERVICE)}")
    assert exc.value.status_code in (401, 403)


def test_rate_limit_trips_and_reports_the_right_code():
    from coursemate_contracts.errors import ErrorCode
    from coursemate_service.api.deps import _RateLimiter
    from coursemate_service.config import settings

    limiter = _RateLimiter()
    for _ in range(settings.student_requests_per_minute):
        limiter.check("u1")

    with pytest.raises(HTTPException) as exc:
        limiter.check("u1")
    assert exc.value.status_code == 429
    # Typed, so the UI can say "give it a moment" rather than "error".
    assert exc.value.detail == ErrorCode.RATE_LIMITED.value


def test_rate_limit_is_per_student():
    from coursemate_service.api.deps import _RateLimiter
    from coursemate_service.config import settings

    limiter = _RateLimiter()
    for _ in range(settings.student_requests_per_minute):
        limiter.check("noisy")
    limiter.check("quiet")  # must not raise
