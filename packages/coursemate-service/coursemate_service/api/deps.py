"""Request dependencies — identity, authorization, rate limiting.

Design §3.4 and §10.1. Two separate credential classes, and keeping them apart is
the point: a leaked student-path token must not be able to write to the index.

The rule that matters most here is §10.1: **authorization is inherited, never
reinvented.** The JWT establishes *who is asking*. It is not a grant of access.
Enrollment and role are re-checked at the boundary on every call, against the
platform, so a forged or replayed claim of enrollment buys nothing.

That mattered before v8 and matters more now: since the browser holds the token
directly, anything the token asserts is attacker-controlled in the threat model.
"""

from __future__ import annotations

import time

import jwt
from coursemate_contracts.auth import AUDIENCE_SERVICE, AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.errors import ErrorCode
from fastapi import Depends, Header, HTTPException, status

from ..config import settings

ALGORITHM = "HS256"


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorCode.UNAUTHENTICATED.value,
        )
    return authorization.split(" ", 1)[1].strip()


def student_claims(authorization: str | None = Header(default=None)) -> StudentClaims:
    """Verify signature, expiry and audience on **every** request.

    Never trusts a caller-supplied identity, and never caches the verification:
    the whole point of a short expiry is lost if a token is honoured after it.
    """
    token = _bearer(authorization)
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE_STUDENT,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail=ErrorCode.UNAUTHENTICATED.value)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail=ErrorCode.UNAUTHENTICATED.value)

    if payload.get("aud") == AUDIENCE_SERVICE:
        # A service credential must never be usable on the student path.
        raise HTTPException(status_code=403, detail=ErrorCode.UNAUTHENTICATED.value)

    return StudentClaims(**payload)


def service_credential(authorization: str | None = Header(default=None)) -> None:
    """Ingest and invalidation. A separate secret from the student path (§3.4)."""
    token = _bearer(authorization)
    # Constant-time compare: this is a fixed shared secret, so a timing oracle
    # would be a real leak rather than a theoretical one.
    import hmac

    if not hmac.compare_digest(token, settings.service_credential):
        raise HTTPException(status_code=401, detail=ErrorCode.UNAUTHENTICATED.value)


class _RateLimiter:
    """Per-student limit, enforced here rather than in the XBlock.

    §10.8 wants these "at the boundary alongside authorization, so a new agent
    node cannot bypass them." Since v8 that is also where they have to live: the
    XBlock is out of the answer path, so it is no longer in a position to count
    anything.

    In-memory and per-process, which is honest about what it is: adequate for a
    single-replica MVP, and the thing to move to Redis the moment there are two.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(self, student_id: str) -> None:
        now = time.time()
        window = self._hits.setdefault(student_id, [])
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= settings.student_requests_per_minute:
            raise HTTPException(status_code=429, detail=ErrorCode.RATE_LIMITED.value)
        window.append(now)


rate_limiter = _RateLimiter()


def rate_limited(claims: StudentClaims = Depends(student_claims)) -> StudentClaims:
    rate_limiter.check(claims.sub)
    return claims
