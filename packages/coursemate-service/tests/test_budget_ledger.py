"""The per-student daily spend ceiling, on both storage paths and in the pipeline.

Three properties are worth more than the rest and each has a test whose failure
would be silent otherwise:

* the provider is **not called** when a student is over budget — a ceiling that
  refuses after paying is a log line, not a limit;
* a Redis outage does not become unlimited spending;
* an abstention or a dead provider does not bill anyone, because a ledger that
  charges for "I don't know" is a request counter with a token label on it.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, Citation, FrameType
from coursemate_contracts.errors import ErrorCode
from coursemate_service import budget as B
from coursemate_service import shared_state
from coursemate_service.ai import client as ai_client
from coursemate_service.ai import pipeline as pl
from coursemate_service.ai.context import ContextChunk, ContextResult

OFFERING = "course-v1:X+Y+Z"
OTHER_OFFERING = "course-v1:X+Y+OTHER"


class FakeRedis:
    """Enough Redis for a counter: get, incrby, expire, and a pipeline."""

    def __init__(self) -> None:
        self.kv: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    def get(self, k):
        if self.fail:
            raise ConnectionError("down")
        v = self.kv.get(k)
        return str(v) if v is not None else None

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r, self.ops = r, []

    def incrby(self, key, n):
        self.ops.append(("incr", key, n))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        if self.r.fail:
            raise ConnectionError("down")
        out = []
        for op in self.ops:
            if op[0] == "incr":
                _, key, n = op
                self.r.kv[key] = self.r.kv.get(key, 0) + n
                out.append(self.r.kv[key])
            else:
                _, key, ttl = op
                self.r.ttl[key] = ttl
                out.append(True)
        return out


@pytest.fixture(autouse=True)
def _isolate():
    """Every test starts with an empty ledger and no memoised client."""
    shared_state.reset_for_tests()
    B.ledger.reset_for_tests()
    yield
    shared_state.reset_for_tests()
    B.ledger.reset_for_tests()


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(shared_state, "get_redis", lambda: fake)
    return fake


def _claims(sub="u1", offering=OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, course_id=offering, offering_id=offering, roles=["student"],
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


# =============================================================== the ledger ==


def test_a_fresh_student_has_spent_nothing(redis):
    assert B.ledger.spent(OFFERING, "u1") == 0
    assert B.ledger.would_exceed(OFFERING, "u1") is False


def test_below_the_limit_is_allowed(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 999)
    assert B.ledger.would_exceed(OFFERING, "u1") is False


def test_exactly_at_the_limit_is_refused(redis, monkeypatch):
    """The boundary, stated once so it cannot drift.

    `>=`, not `>`. A student who has spent exactly the ceiling is done; the
    alternative makes `student_daily_token_budget` mean "the limit, plus one
    more question", and the extra question is the expensive one because nothing
    caps how large it is."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    assert B.ledger.would_exceed(OFFERING, "u1") is True


def test_one_token_over_is_refused(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1001)
    assert B.ledger.would_exceed(OFFERING, "u1") is True


def test_repeated_requests_accumulate(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    for _ in range(4):
        assert B.ledger.would_exceed(OFFERING, "u1") is False
        B.ledger.record(OFFERING, "u1", 250)
    assert B.ledger.spent(OFFERING, "u1") == 1000
    assert B.ledger.would_exceed(OFFERING, "u1") is True


def test_two_students_do_not_share_a_budget(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    assert B.ledger.would_exceed(OFFERING, "u1") is True
    assert B.ledger.would_exceed(OFFERING, "u2") is False
    assert B.ledger.spent(OFFERING, "u2") == 0


def test_the_same_student_in_two_courses_does_not_leak(redis, monkeypatch):
    """Offering is a tenant boundary everywhere else in the service and is one
    here too. Exhausting one course must not silence the tutor in another."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    assert B.ledger.would_exceed(OFFERING, "u1") is True
    assert B.ledger.would_exceed(OTHER_OFFERING, "u1") is False


def test_the_key_names_student_offering_and_day():
    key = B.budget_key(OFFERING, "u1")
    assert key.startswith("cm:budget:")
    assert OFFERING in key and "u1" in key
    assert key.split(":")[-1] == B._today()


def test_the_budget_key_cannot_collide_with_the_rate_limit_keys():
    """Regression guard for the reuse decision. The ledger shares one Redis
    client with the limiter, so the namespaces have to stay disjoint — a
    collision would make a rate-limit sorted set and a budget counter the same
    key, and Redis would raise WRONGTYPE at some unrelated moment."""
    key = B.budget_key(OFFERING, "u1")
    assert not key.startswith("cm:rl:")
    assert not key.startswith("cm:stream:")


def test_a_zero_ceiling_disables_rather_than_refuses_everyone(redis, monkeypatch):
    """A mis-set or unset limit must not take the tutor offline. Failing that
    way would make this feature the most likely cause of a total outage."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 0)
    B.ledger.record(OFFERING, "u1", 10_000_000)
    assert B.ledger.would_exceed(OFFERING, "u1") is False


def test_recording_zero_or_negative_tokens_is_a_no_op(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 0)
    B.ledger.record(OFFERING, "u1", -5)
    assert B.ledger.spent(OFFERING, "u1") == 0


def test_the_counter_is_given_a_ttl(redis, monkeypatch):
    """The date in the key rolls the budget over; the TTL only reclaims the
    memory. Without it every student-day ever seen stays in Redis forever."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 10)
    assert redis.ttl[B.budget_key(OFFERING, "u1")] == B._KEY_TTL_SECONDS


# ====================================================== redis is not there ==


def test_with_no_redis_the_ceiling_still_applies(monkeypatch):
    """Single-replica deployments have no Redis configured at all. The ceiling
    is not optional there."""
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    assert B.ledger.would_exceed(OFFERING, "u1") is True


def test_a_redis_outage_is_not_unlimited_spending(redis, monkeypatch):
    """**The documented failure policy, pinned.**

    Redis going down must not hand every student an unmetered tutor. It also
    must not refuse them — that turns a cache outage into a total outage, the
    trade `shared_state` already rejected for the rate limiter.

    The third option is what is implemented: fall back to per-process counting.
    The ceiling still binds against the local tally, so the worst case during an
    outage is `replicas x ceiling` rather than infinity, and one replica — this
    deployment — degrades to exactly the ceiling.
    """
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    redis.fail = True

    for _ in range(4):
        B.ledger.record(OFFERING, "u1", 250)

    assert B.ledger.spent(OFFERING, "u1") == 1000, "the charge vanished with Redis"
    assert B.ledger.would_exceed(OFFERING, "u1") is True, "an outage lifted the ceiling"


def test_a_write_during_an_outage_is_not_dropped(redis, monkeypatch):
    """The specific way this would go wrong quietly: the Redis write raises, the
    exception is swallowed as 'degraded', and the tokens are never counted
    anywhere. Spending would then be unlimited for the length of the outage
    while every log line said the fallback was working."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    redis.fail = True
    B.ledger.record(OFFERING, "u1", 400)
    assert B.ledger.spent(OFFERING, "u1") == 400


def test_the_local_tally_drops_other_days(monkeypatch):
    """The leak guard, which only runs on the fallback path and would otherwise
    never be exercised. Without it a long-lived process keeps one counter per
    student per day forever — invisible until memory surfaces it, which is the
    same slow leak `_RateLimiter._check_local` guards against."""
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)

    B.ledger._local.update({f"cm:budget:{OFFERING}:u{i}:19990101": 5 for i in range(1200)})
    B.ledger.record(OFFERING, "today-student", 10)

    assert not [k for k in B.ledger._local if k.endswith("19990101")]
    assert B.ledger.spent(OFFERING, "today-student") == 10, "today's charge was pruned"


def test_recording_never_raises(redis, monkeypatch):
    """It runs after the answer has streamed. An exception here would break a
    request that had already succeeded."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    redis.fail = True
    B.ledger.record(OFFERING, "u1", 10)  # must not raise


# ================================================== the estimate fallback ==


def test_the_estimate_counts_prompt_and_answer():
    msgs = [{"role": "system", "content": "a" * 400}, {"role": "user", "content": "b" * 400}]
    assert B.estimate_tokens(msgs, ["c" * 200]) == 250  # 1000 chars // 4


def test_the_estimate_is_never_zero_for_real_text():
    """A zero would be recorded as 'no charge' by `record`, so a tiny answer
    would be free — and many tiny answers would be free forever."""
    assert B.estimate_tokens([{"role": "user", "content": "hi"}], ["ok"]) >= 1


# =========================================================== the pipeline ==


class _Grounded:
    async def fetch(self, question, claims):
        return ContextResult(
            chunks=[ContextChunk(text="t", citation=Citation(usage_key="u"), score=0.9)],
            top_score=0.9,
        )


class _Chunk:
    def __init__(self, text, finish=None, usage=None):
        self.choices = [type("C", (), {
            "delta": type("D", (), {"content": text})(),
            "finish_reason": finish,
        })()]
        self.model = "test-model"
        if usage is not None:
            self.usage = usage


class _UsageOnlyChunk:
    """What a provider actually sends last when it reports usage: no choices."""

    def __init__(self, total):
        self.choices = []
        self.model = "test-model"
        self.usage = type("U", (), {"total_tokens": total})()


def _router(chunks, calls):
    async def _stream():
        for c in chunks:
            yield c

    class _Router:
        async def acompletion(self, **kw):
            calls.append(kw)
            return _stream()

    return _Router()


def _install(monkeypatch, chunks):
    calls: list = []
    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router", lambda: _router(chunks, calls))
    monkeypatch.setattr(pl.settings, "require_grounding", True)
    return calls


async def _collect(request, claims):
    return [f async for f in pl.AnswerPipeline(_Grounded()).stream(request, claims)]


@pytest.mark.asyncio
async def test_under_budget_the_answer_streams(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)
    calls = _install(monkeypatch, [_Chunk("an answer", "stop")])

    frames = await _collect(ChatRequest(question="hi"), _claims())

    assert FrameType.TOKEN in [f.type for f in frames]
    assert len(calls) == 1, "the provider was not called"


@pytest.mark.asyncio
async def test_over_budget_refuses_and_never_calls_the_provider(redis, monkeypatch):
    """**The test the whole feature rests on.** A ceiling enforced after the
    provider call would still be billed for every refusal."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    calls = _install(monkeypatch, [_Chunk("an answer", "stop")])

    frames = await _collect(ChatRequest(question="hi"), _claims())

    assert calls == [], "the provider was called for a student who is over budget"
    assert frames[-1].type == FrameType.ERROR
    assert frames[-1].error_code == ErrorCode.BUDGET_EXCEEDED
    assert not [f for f in frames if f.type == FrameType.TOKEN]


@pytest.mark.asyncio
async def test_the_refusal_is_scoped_to_the_student_who_spent(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 1000)
    calls = _install(monkeypatch, [_Chunk("an answer", "stop")])

    frames = await _collect(ChatRequest(question="hi"), _claims(sub="u2"))

    assert len(calls) == 1
    assert FrameType.TOKEN in [f.type for f in frames]


@pytest.mark.asyncio
async def test_provider_reported_usage_is_what_gets_charged(redis, monkeypatch):
    """The real number, taken from the chunk that carries no choices — the one
    the `choices` guard would skip if usage were read further down."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)
    _install(monkeypatch, [_Chunk("an answer", "stop"), _UsageOnlyChunk(4321)])

    await _collect(ChatRequest(question="hi"), _claims())

    assert B.ledger.spent(OFFERING, "u1") == 4321


@pytest.mark.asyncio
async def test_without_a_provider_report_the_estimate_is_charged(redis, monkeypatch):
    """The local Ollama path reports no usage for some model families. Charging
    nothing there would make an unmetered tutor out of a deployment detail."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)
    _install(monkeypatch, [_Chunk("an answer", "stop")])

    await _collect(ChatRequest(question="hi"), _claims())

    assert B.ledger.spent(OFFERING, "u1") > 0


@pytest.mark.asyncio
async def test_spend_accumulates_across_turns(redis, monkeypatch):
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)
    _install(monkeypatch, [_Chunk("an answer", "stop"), _UsageOnlyChunk(1000)])

    await _collect(ChatRequest(question="one"), _claims())
    await _collect(ChatRequest(question="two"), _claims())

    assert B.ledger.spent(OFFERING, "u1") == 2000


@pytest.mark.asyncio
async def test_an_abstention_bills_nobody(redis, monkeypatch):
    """Abstention costs no latency and no spend, and that claim is load-bearing
    in `ai/query.py` and the design. It must stay true now that something counts."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)
    calls = _install(monkeypatch, [_Chunk("an answer", "stop")])
    # Above the 0.9 context score, so the gate abstains before anything is spent.
    monkeypatch.setattr(pl.settings, "confidence_threshold", 0.95)

    frames = await _collect(ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code == ErrorCode.ABSTAINED
    assert calls == []
    assert B.ledger.spent(OFFERING, "u1") == 0


@pytest.mark.asyncio
async def test_a_provider_failure_with_no_output_bills_nobody(redis, monkeypatch):
    """A dead provider must not consume the student's day. Otherwise an outage
    silently spends everyone's budget and the tutor stays down after it ends."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)

    async def _boom():
        raise RuntimeError("provider down")
        yield  # pragma: no cover

    class _Router:
        async def acompletion(self, **kw):
            return _boom()

    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router", lambda: _Router())
    monkeypatch.setattr(pl.settings, "require_grounding", True)

    frames = await _collect(ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code == ErrorCode.UNAVAILABLE
    assert B.ledger.spent(OFFERING, "u1") == 0


@pytest.mark.asyncio
async def test_a_failure_after_partial_output_is_still_charged(redis, monkeypatch):
    """Those tokens were generated and will appear on the bill. Not charging
    them would make a provider that dies mid-answer the cheapest way to use the
    tutor — and the student still received text."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 100_000)

    async def _partial():
        yield _Chunk("half an answer", None)
        raise RuntimeError("connection reset")

    class _Router:
        async def acompletion(self, **kw):
            return _partial()

    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router", lambda: _Router())
    monkeypatch.setattr(pl.settings, "require_grounding", True)

    frames = await _collect(ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code == ErrorCode.UNAVAILABLE
    assert B.ledger.spent(OFFERING, "u1") > 0


@pytest.mark.asyncio
async def test_the_budget_frame_is_the_normal_error_contract(redis, monkeypatch):
    """No new frame type and no new field — the UI renders it through the same
    path as every other typed error."""
    monkeypatch.setattr(B.settings, "student_daily_token_budget", 1000)
    B.ledger.record(OFFERING, "u1", 5000)
    _install(monkeypatch, [_Chunk("x", "stop")])

    frame = (await _collect(ChatRequest(question="hi"), _claims()))[-1]

    assert frame.type == FrameType.ERROR
    assert frame.error_code == ErrorCode.BUDGET_EXCEEDED
    assert frame.text is None
