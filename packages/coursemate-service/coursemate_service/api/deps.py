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

import logging
import time
import uuid
from collections.abc import AsyncIterator

import jwt
from coursemate_contracts.auth import AUDIENCE_SERVICE, AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.errors import ErrorCode
from fastapi import Depends, Header, HTTPException, status

from .. import shared_state
from ..config import settings

log = logging.getLogger(__name__)

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

#: How long a stream slot survives without being released. This is a **lease**,
#: not a timeout on the stream: if a worker is killed mid-generation the `finally`
#: that releases the slot never runs, and without an expiry that student would be
#: locked out of practice until the process restarted. A leak that only ever
#: denies service, with nothing logged, is the failure shape this project keeps
#: finding — so slots expire on their own and the release is an optimisation.
#:
#: Comfortably longer than any real generation (one model call, seconds) and far
#: shorter than a student's patience for being told to wait.
_STREAM_LEASE_SECONDS = 180


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
        #: student -> {slot token: acquired-at}. Concurrency, not rate — see
        #: `acquire_stream`.
        self._streams: dict[str, dict[str, float]] = {}

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

    # --- concurrency slots -------------------------------------------------
    #
    # A *rate* limit and a *concurrency* limit answer different questions, and
    # the sliding window above cannot answer the second one: its entries expire
    # by clock, so it can say "20 requests this minute" but never "2 streams open
    # right now". A stream that ends after 300 ms and one still running both
    # count identically to it.
    #
    # So this is the same limiter — one class, one Redis client, one fail-open
    # policy, one typed error — given the acquire/release pair concurrency needs.
    # A second limiter component would mean two places to keep those four
    # decisions consistent, and they would drift.
    #
    # Why the limit exists: each practice stream holds an upstream model call for
    # its whole life. Rate limiting caps how often a student *starts* one; only
    # this caps how many they hold open at once, which is what actually consumes
    # the provider's concurrency budget for everyone else on the instance.

    def _acquire_redis(self, client, student_id: str, token: str, now: float) -> bool:
        """One round trip: trim expired leases, count, insert, re-arm the TTL.

        Counting in a separate call from the insert is how a limiter lets through
        more than it should under exactly the concurrency it exists to limit —
        the same reason `_check_redis` is pipelined.
        """
        key = f"cm:stream:{student_id}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, now - _STREAM_LEASE_SECONDS)
        pipe.zcard(key)
        pipe.zadd(key, {token: now})
        pipe.expire(key, _STREAM_LEASE_SECONDS * 2)
        _, count, _, _ = pipe.execute()
        if count < settings.max_concurrent_streams:
            return True
        # Over the limit: take back the slot this call just inserted. Inserting
        # first and removing on refusal keeps the count-and-insert atomic; the
        # alternative — check, then insert — is the race.
        client.zrem(key, token)
        return False

    def _acquire_local(self, student_id: str, token: str, now: float) -> bool:
        slots = self._streams.setdefault(student_id, {})
        for t, started in list(slots.items()):
            if now - started > _STREAM_LEASE_SECONDS:
                slots.pop(t, None)
        if len(slots) >= settings.max_concurrent_streams:
            if not slots:
                self._streams.pop(student_id, None)
            return False
        slots[token] = now
        return True

    def acquire_stream(self, student_id: str) -> str:
        """Take a concurrency slot, or raise 429. Returns the release token.

        Fails OPEN when Redis is unreachable, matching `check`: this is abuse
        control, not authorization, and refusing every student because a cache is
        down trades a small abuse risk for a total outage.
        """
        now = time.time()
        token = uuid.uuid4().hex
        client = shared_state.get_redis()
        if client is not None:
            try:
                allowed = self._acquire_redis(client, student_id, token, now)
            except Exception as exc:  # noqa: BLE001
                shared_state.redis_failed("stream concurrency", exc)
                allowed = self._acquire_local(student_id, token, now)
        else:
            allowed = self._acquire_local(student_id, token, now)

        if not allowed:
            raise HTTPException(status_code=429, detail=ErrorCode.RATE_LIMITED.value)
        return token

    def release_stream(self, student_id: str, token: str) -> None:
        """Give the slot back. Safe to call twice, and safe to never call.

        Never raises. It runs in a `finally` on the streaming path, where an
        exception would replace whatever real error ended the stream with a
        bookkeeping one — and the lease expiry already covers the case where it
        does not run at all.
        """
        client = shared_state.get_redis()
        if client is not None:
            try:
                client.zrem(f"cm:stream:{student_id}", token)
                return
            except Exception as exc:  # noqa: BLE001
                shared_state.redis_failed("stream release", exc)
        slots = self._streams.get(student_id)
        if slots is not None:
            slots.pop(token, None)
            if not slots:
                # Drop the empty dict, or this grows one key per student ever
                # seen — the same slow leak `_check_local` had to be fixed for.
                self._streams.pop(student_id, None)

    def active_streams(self, student_id: str) -> int:
        """Currently held slots. For tests and diagnostics, not for deciding."""
        now = time.time()
        client = shared_state.get_redis()
        if client is not None:
            try:
                client.zremrangebyscore(
                    f"cm:stream:{student_id}", 0, now - _STREAM_LEASE_SECONDS
                )
                return int(client.zcard(f"cm:stream:{student_id}"))
            except Exception as exc:  # noqa: BLE001
                # Diagnostic only — nothing decides on this, so falling back to
                # the local view is better than propagating a cache failure.
                log.debug("stream count unavailable from redis: %s", exc)
        slots = self._streams.get(student_id, {})
        return sum(1 for t in slots.values() if now - t <= _STREAM_LEASE_SECONDS)


rate_limiter = _RateLimiter()


def rate_limited(claims: StudentClaims = Depends(student_claims)) -> StudentClaims:
    rate_limiter.check(claims.sub)
    return claims


async def holding_stream_slot(
    source: AsyncIterator[str], student_id: str, token: str
) -> AsyncIterator[str]:
    """Pass an encoded stream through, giving the slot back when it ends.

    The `finally` covers all three endings that matter: the generator finished,
    it raised, or the student closed the tab — Starlette closes the iterator on
    disconnect, which arrives here as `GeneratorExit` and still runs the release.

    **Releasing here rather than in the endpoint is the whole point.** The
    endpoint returns as soon as the `StreamingResponse` is constructed, long
    before a single token has been generated, so a release there would free the
    slot while the stream it was protecting was still running.

    It lives beside the limiter rather than in a route module because both
    streaming routes need it and the release policy is one decision. It was
    `examprep._encode_holding_slot` while only one route had a slot; chat gained
    one on 2026-08-12 and a second copy of a `finally` is a second copy of the
    rule about when a slot is safe to give back.

    Deliberately generic over already-encoded strings, so it needs no opinion
    about SSE framing and no import from either route module.
    """
    try:
        async for chunk in source:
            yield chunk
    finally:
        rate_limiter.release_stream(student_id, token)
