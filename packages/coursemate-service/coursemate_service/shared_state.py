"""One Redis client, shared by the rate limiter and the authz cache.

Both were in-memory and both were wrong the moment a second replica existed —
the limiter allowed N times the limit, and invalidating an entitlement cleared
one process's dictionary while the others kept serving. Neither failure raises
anything; they are only visible as "the limit does not work" and "unenrolling
did not take effect", which is why they sat unnoticed.

Redis is already in every Tutor deployment as Celery's broker, so this adds no
infrastructure.

**Degrades rather than fails.** If `redis_url` is unset, or Redis is
unreachable, callers fall back to per-process state — the previous behaviour,
which is correct for one replica. The alternative, refusing to serve when a
*cache* is down, would turn an optimisation into an outage. That is the opposite
choice from `boundary/authz.py`, and deliberately so: authorization fails closed
because being wrong there leaks content, while a cache failing closed only
denies people who are entitled.
"""

from __future__ import annotations

import logging
import time

from .config import settings

log = logging.getLogger(__name__)

_client = None
_resolved = False
_warned = False
#: When a failed resolution may be tried again. See `get_redis`.
_retry_after: float = 0.0

#: How long a failed connection is remembered before another attempt. Long
#: enough that a genuine outage is not re-probed on every request; short enough
#: that a container which started during a Redis blip recovers by itself.
_RESOLVE_RETRY_SECONDS = 30.0


def get_redis():
    """The shared client, or None when running single-process.

    **A failed connection is retried, not remembered forever.** This used to set
    `_resolved = True` before attempting the connection and never clear it, so
    Redis being unreachable at the moment of the first call pinned `_client` to
    None for the life of the process. Redis coming back changed nothing until
    someone restarted uvicorn, and four things stayed silently per-process:

      * the rate limiter — N replicas each allow the full limit,
      * the authz cache, and with it `invalidate()`, which is a SECURITY control:
        a revoked student keeps working on every replica that did not get the
        notice,
      * the daily token ledger,
      * the response cache, which simply always misses.

    One `log.error` at startup was the only trace, and it is honest about the
    consequence — but a container that survives a thirty-second Redis blip
    inherits that state permanently, and nothing says so again.

    A SUCCESSFUL resolution is still permanent: the client handles its own
    reconnection, and re-resolving a working connection would be churn. Only
    failure is retried, and only after a cooldown — a per-request connection
    attempt during a real outage would add a timeout to every student's request,
    which is the outage this module refuses to create.

    Failures are reported once rather than on every attempt: a per-request log
    line during an outage buries the thing that actually matters in the same
    file.
    """
    global _client, _resolved, _warned, _retry_after
    if _resolved:
        return _client
    if _retry_after and time.monotonic() < _retry_after:
        return None
    _resolved = True

    if not settings.redis_url:
        log.info("coursemate: no redis_url set; rate limit and authz cache are per-process")
        return None

    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001
        if not _warned:
            _warned = True
            log.error(
                "coursemate: redis at %s unreachable (%s); falling back to "
                "per-process state and retrying every %.0fs. This is correct for "
                "one replica and WRONG for more than one.",
                settings.redis_url, type(exc).__name__, _RESOLVE_RETRY_SECONDS,
            )
        # Re-open the question rather than settling it. `_resolved` stays False
        # so the next call past the cooldown attempts a real connection.
        _client = None
        _resolved = False
        _retry_after = time.monotonic() + _RESOLVE_RETRY_SECONDS
        return None

    log.info("coursemate: shared state via redis at %s", settings.redis_url)
    _client = client
    # A later outage may re-arm the warning, so a SECOND degradation is reported
    # rather than swallowed by the first one's flag.
    _warned = False
    _retry_after = 0.0
    return _client


def redis_failed(operation: str, exc: Exception) -> None:
    """Report a mid-flight Redis failure once, then stay quiet."""
    global _warned
    if not _warned:
        _warned = True
        log.error(
            "coursemate: redis %s failed (%s); degrading to per-process state",
            operation, type(exc).__name__,
        )


def reset_for_tests() -> None:
    """Drop the memoised client so a test can point at a different URL.

    `_retry_after` is cleared too. Without it a test that exercised a failed
    connection would leave the cooldown armed, and the NEXT test — pointing at a
    working URL — would get None back and pass or fail for a reason that has
    nothing to do with what it was checking.
    """
    global _client, _resolved, _warned, _retry_after
    _client, _resolved, _warned = None, False, False
    _retry_after = 0.0
