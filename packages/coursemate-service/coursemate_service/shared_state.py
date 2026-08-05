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

from .config import settings

log = logging.getLogger(__name__)

_client = None
_resolved = False
_warned = False


def get_redis():
    """The shared client, or None when running single-process.

    Resolved once. A connection failure is reported one time rather than on
    every request — a per-request log line during a Redis outage buries the
    thing that actually matters in the same file.
    """
    global _client, _resolved, _warned
    if _resolved:
        return _client
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
        log.error(
            "coursemate: redis at %s unreachable (%s); falling back to per-process "
            "state. This is correct for one replica and WRONG for more than one.",
            settings.redis_url, type(exc).__name__,
        )
        _client = None
        return None

    log.info("coursemate: shared state via redis at %s", settings.redis_url)
    _client = client
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
    """Drop the memoised client so a test can point at a different URL."""
    global _client, _resolved, _warned
    _client, _resolved, _warned = None, False, False
