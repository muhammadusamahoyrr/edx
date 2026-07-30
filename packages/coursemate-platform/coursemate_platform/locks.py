"""In-flight locks and cooldowns for expensive, user-triggerable jobs.

Design §5.1. The query-time bootstrap backstop is the reason this exists: as first
drafted it let any student queue a full course re-index by refreshing the page.
Three controls, and all three are here rather than at the call sites, so a new
trigger cannot forget one:

* **in-flight lock** — at most one bootstrap per course at a time; repeat requests
  attach to the running job rather than queueing another.
* **cooldown** — a failed run cannot be re-triggered in a tight loop.
* **cost ceiling** — enforced per course (§10.8), checked by the task itself.

The student-facing message is identical whether their request started the job or
joined one already running.
"""

from __future__ import annotations

from django.core.cache import cache

#: Long enough to cover a large course, short enough that a crashed worker does
#: not wedge the course forever.
BOOTSTRAP_LOCK_TTL_SECONDS = 60 * 30

#: After a failure, wait this long before another attempt is allowed.
BOOTSTRAP_COOLDOWN_SECONDS = 60 * 5


class _BootstrapLock:
    def _key(self, course_id: str) -> str:
        return f"coursemate:bootstrap:lock:{course_id}"

    def _cooldown_key(self, course_id: str) -> str:
        return f"coursemate:bootstrap:cooldown:{course_id}"

    def acquire(self, course_id: str) -> bool:
        """True if this caller started the job; False if one is already running
        or the course is cooling down after a failure.

        `cache.add` is atomic, which is what makes two simultaneous clicks safe.
        """
        if cache.get(self._cooldown_key(course_id)):
            return False
        return bool(cache.add(self._key(course_id), "1", BOOTSTRAP_LOCK_TTL_SECONDS))

    def release(self, course_id: str) -> None:
        cache.delete(self._key(course_id))

    def mark_failed(self, course_id: str) -> None:
        """Release the lock but start a cooldown, so a persistently failing course
        cannot be hammered by a page refresh."""
        self.release(course_id)
        cache.set(self._cooldown_key(course_id), "1", BOOTSTRAP_COOLDOWN_SECONDS)

    def is_running(self, course_id: str) -> bool:
        return bool(cache.get(self._key(course_id)))


bootstrap_lock = _BootstrapLock()
