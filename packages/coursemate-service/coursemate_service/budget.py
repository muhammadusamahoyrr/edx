"""A per-student daily token ceiling for the chat answer path.

Rate limiting caps how OFTEN a student asks; concurrency caps how many streams
they hold open. Neither bounds total spend: twenty questions a minute, all day,
is within both limits and is the shape of the bill nobody notices until it
arrives. This is the third question — how much a single student can cost in a
day — and it is the only one of the three whose answer is money.

**Tokens, not dollars.** A dollar ceiling needs a price per model, and that table
is wrong the moment a provider reprices or the router falls back to a different
deployment. Tokens are what the provider actually meters, they are comparable
across deployments, and they do not rot. `docs/BENCHMARKS.md` can translate to
money when someone wants a figure; the enforcement does not need to.

**No new storage.** `shared_state.get_redis()` is the client the rate limiter and
the authz cache already share, with the same resolution and the same one-time
failure logging. A second client would be a second place to configure timeouts
and a second thing to notice was down.

**Redis failure degrades to per-process counting, and that is deliberate.** The
tempting reading of "do not let an outage mean unlimited spend" is to fail closed
— refuse everyone while Redis is down. That turns a cache outage into a total
tutor outage, which is the trade `shared_state` already rejected for the limiter.
Per-process counting is the third option and is bounded: each replica still
enforces the full ceiling against its own tally, so the worst case during an
outage is `replicas x ceiling` rather than infinity. With this deployment's
single service replica the worst case is exactly the ceiling. That reasoning is
pinned by `test_budget_ledger.py::test_a_redis_outage_is_not_unlimited_spending`
rather than left in a comment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import shared_state
from .config import settings

log = logging.getLogger(__name__)

#: Long enough that a key outlives the day it counts even with clock skew, short
#: enough that yesterday's counters do not accumulate. The DATE in the key is
#: what rolls the budget over; this only reclaims memory afterwards.
_KEY_TTL_SECONDS = 48 * 60 * 60


def _today() -> str:
    """UTC, always.

    Local time would make the reset point depend on which machine answered, and
    with replicas in two regions a student could cross midnight twice or not at
    all. The student-facing message says UTC for the same reason.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def budget_key(offering_id: str, student_id: str) -> str:
    """`cm:budget:{offering}:{student}:{yyyymmdd}`.

    **Scoped per offering as well as per student**, matching every other tenant
    boundary in the service. A student enrolled in two courses gets a ceiling in
    each, which is the same answer the enrollment check gives everywhere else:
    the two courses are separate tenants and neither can consume the other's
    allowance. Merging them into one global counter would let a busy course
    silently exhaust a quiet one.

    The identity here must come from verified claims. Nothing in this module
    reads a request body — see the call site in `ai/pipeline.py`.
    """
    return f"cm:budget:{offering_id}:{student_id}:{_today()}"


#: Characters per token. Four is the usual English approximation and is only
#: reached for when the provider volunteers nothing.
_CHARS_PER_TOKEN = 4


def estimate_tokens(messages: list[dict], answer_parts: list[str]) -> int:
    """A fallback count over the text that really moved, when the provider is silent.

    **This is an estimate and is named one.** It is reached only when a provider
    reports no usage, which the local Ollama deployment does for some model
    families. The alternative on that path is to charge nothing, and a provider
    that happens not to report usage would then offer an unmetered tutor —
    exactly the hole the ceiling exists to close.

    It measures the ACTUAL prompt and the ACTUAL generated text, so it is a
    measurement of work done, not a request counter with a token label on it. It
    will be wrong in the third significant figure, and the ceiling is set with
    enough headroom that being wrong there does not matter. What it cannot do is
    be wrong about whether a thing happened at all, which is the property the
    ledger depends on.
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    chars += sum(len(part) for part in answer_parts)
    return max(1, chars // _CHARS_PER_TOKEN)


class DailyTokenLedger:
    """Tokens spent per student per course per UTC day.

    Two operations, deliberately not one: `would_exceed` runs BEFORE the provider
    call and `record` runs after it, because the true cost is not known until the
    answer exists. Reserving an estimate up front instead would charge
    abstentions and provider failures for tokens nobody spent, which is a worse
    error: it bills students for the system saying "I don't know".

    The consequence is that the last permitted question can overshoot the ceiling
    by one answer. **How far is not currently bounded by the contract**, and an
    earlier version of this docstring claimed otherwise — it said "bounded by
    `max_output_tokens` plus its prompt", which is only true if the prompt has a
    limit. The reply does (`max_output_tokens`), but `ChatRequest.question` and
    `Turn.content` have no `max_length`; only the turn *count* is capped, at 20.
    So a single request's prompt is student-controlled and unbounded by anything
    here, and whatever practical limit exists comes from the web server and has
    not been measured.

    In practice the overshoot is one answer's worth of a normal question, and it
    is self-limiting — an oversized request spends the sender's own day. Closing
    it properly means putting a length cap on those two fields, which is a
    contract decision rather than a change to this module.
    """

    def __init__(self) -> None:
        #: Per-process fallback, keyed the same way so the two paths cannot
        #: disagree about scope. Only reached when Redis is absent or failing.
        self._local: dict[str, int] = {}

    # --- reads ---------------------------------------------------------------

    def spent(self, offering_id: str, student_id: str) -> int:
        key = budget_key(offering_id, student_id)
        client = shared_state.get_redis()
        if client is not None:
            try:
                raw = client.get(key)
                return int(raw) if raw else 0
            except Exception as exc:  # noqa: BLE001
                shared_state.redis_failed("spend ledger read", exc)
        return self._local.get(key, 0)

    def would_exceed(self, offering_id: str, student_id: str) -> bool:
        """Whether this student has already used their day.

        The comparison is `>=`: a student who has spent exactly the ceiling is
        done. Anything else would make the configured number mean "the limit,
        plus one more question", which is not what a limit called
        `student_daily_token_budget` says.

        A ceiling of 0 or below disables the check entirely rather than refusing
        every request — an unset or mis-set limit must not silently take the
        tutor offline.
        """
        limit = settings.student_daily_token_budget
        if limit <= 0:
            return False
        return self.spent(offering_id, student_id) >= limit

    # --- writes --------------------------------------------------------------

    def record(self, offering_id: str, student_id: str, tokens: int) -> None:
        """Add real usage to today's tally. Never raises.

        It runs after the student's answer has already streamed, so an exception
        here would turn successful bookkeeping into a failed answer — the student
        would see the tutor break at the moment it had just worked.
        """
        if tokens <= 0:
            return
        key = budget_key(offering_id, student_id)
        client = shared_state.get_redis()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.incrby(key, tokens)
                pipe.expire(key, _KEY_TTL_SECONDS)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                shared_state.redis_failed("spend ledger write", exc)
        # Falling through to the local tally rather than dropping the charge:
        # a write that silently vanishes during an outage is exactly the
        # "unlimited spending" this module exists to prevent.
        self._local[key] = self._local.get(key, 0) + tokens
        self._prune()

    def _prune(self) -> None:
        """Drop other days' local counters.

        Without this the dict keeps one key per student per day for the life of
        the process — the same slow leak `_RateLimiter._check_local` guards
        against, and equally invisible until memory surfaces it.
        """
        if len(self._local) <= 1000:
            return
        today = _today()
        for k in [k for k in self._local if not k.endswith(today)]:
            self._local.pop(k, None)

    def reset_for_tests(self) -> None:
        self._local.clear()


#: One ledger for the process. The Redis path holds no state, and the local
#: fallback must be shared or each caller would count against its own tally.
ledger = DailyTokenLedger()
