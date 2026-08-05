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
import uuid

import jwt
from coursemate_contracts.auth import AUDIENCE_SERVICE, AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.errors import ErrorCode
from fastapi import Depends, Header, HTTPException, status

from ..config import settings
from .. import shared_state

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


#: Sliding window, not a fixed one. A fixed window lets a student spend the whole
#: allowance at 11:59:59 and the whole next allowance at 12:00:00 — twice the
#: limit across two seconds, which is exactly the burst the limit exists to stop.
_WINDOW_SECONDS = 60


class _RateLimiter:
    """Per-student limit, enforced here rather than in the XBlock.

    §10.8 wants these "at the boundary alongside authorization, so a new agent
    node cannot bypass them." Since v8 that is also where they have to live: the
    XBlock is out of the answer path, so it is no longer in a position to count
    anything.

    Shared through Redis when `redis_url` is set. In-memory otherwise, which is
    correct for one replica and silently wrong for two: N replicas each counted
    to the limit independently, so a student got N times the allowance with no
    error anywhere.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    # --- shared path ------------------------------------------------------

    def _check_redis(self, client, student_id: str, now: float) -> bool:
        """True if allowed. A sorted set of request timestamps per student.

        Everything is pipelined so the trim, count, insert and expiry are one
        round trip — under concurrency, reading the count in a separate call
        from the insert is how a limiter lets through more than it should.

        The key expires on its own, which is what keeps this from growing
        without bound the way the in-memory version did.
        """
        key = f"cm:rl:{student_id}"
        # The member must be unique per request. A timestamp alone collides when
        # two requests land in the same clock tick, and a sorted set SILENTLY
        # overwrites a duplicate member rather than erroring — so the collision
        # shows up only as "the limit lets more through than it should", with
        # nothing logged. uuid4 removes the question. (An earlier version keyed
        # on id(pipe), which CPython reuses after GC; the test below caught it.)
        member = f"{now}:{uuid.uuid4().hex}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, now - _WINDOW_SECONDS)
        pipe.zcard(key)
        pipe.zadd(key, {member: now})
        pipe.expire(key, _WINDOW_SECONDS * 2)
        _, count, _, _ = pipe.execute()
        return count < settings.student_requests_per_minute

    # --- per-process fallback ---------------------------------------------

    def _check_local(self, student_id: str, now: float) -> bool:
        window = self._hits.setdefault(student_id, [])
        window[:] = [t for t in window if now - t < _WINDOW_SECONDS]
        if len(window) >= settings.student_requests_per_minute:
            return False
        window.append(now)
        # Drop students who have gone quiet. Without this the dict kept one key
        # per student ever seen, for the life of the process — a slow leak that
        # nothing would surface until memory did.
        if len(self._hits) > 1000:
            for k in [k for k, v in self._hits.items() if not v]:
                self._hits.pop(k, None)
        return True

    def check(self, student_id: str) -> None:
        now = time.time()
        client = shared_state.get_redis()
        if client is not None:
            try:
                allowed = self._check_redis(client, student_id, now)
            except Exception as exc:  # noqa: BLE001
                # Fail OPEN, loudly. Rate limiting is abuse control, not
                # authorization: denying every student because a cache is down
                # trades a small abuse risk for a total outage. Authorization
                # still fails closed, in boundary/authz.py.
                shared_state.redis_failed("rate limit", exc)
                allowed = self._check_local(student_id, now)
        else:
            allowed = self._check_local(student_id, now)

        if not allowed:
            raise HTTPException(status_code=429, detail=ErrorCode.RATE_LIMITED.value)


rate_limiter = _RateLimiter()


def rate_limited(claims: StudentClaims = Depends(student_claims)) -> StudentClaims:
    rate_limiter.check(claims.sub)
    return claims
