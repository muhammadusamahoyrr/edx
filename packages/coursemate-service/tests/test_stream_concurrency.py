"""How many practice streams one student may hold open at once.

A *rate* limit and a *concurrency* limit answer different questions, and the
sliding window cannot answer the second: its entries expire by clock, so a stream
that ended after 300 ms and one still running count identically to it. It can say
"20 requests this minute"; it can never say "2 streams open right now".

Each practice stream holds an upstream model call for its whole life, so the
second limit is the one that bounds how much of the provider's concurrency budget
a single student can occupy while everyone else on the instance waits.

The tests that matter most are the release paths. A slot that is taken and never
given back does not fail loudly — it just means that student can never practise
again, with nothing logged. So: released on success, released on failure,
released on disconnect, and expiring on its own when none of those run.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import FrameType, StreamFrame
from coursemate_contracts.errors import ErrorCode
from coursemate_service.api import deps
from coursemate_service.api.deps import _RateLimiter
from fastapi import HTTPException

OFFERING = "course-v1:OpenedX+OEX101+2024"
OTHER = "course-v1:OpenedX+OEX101+2023"


@pytest.fixture
def limiter(monkeypatch):
    """A fresh limiter with no Redis, so the per-process path is under test."""
    monkeypatch.setattr(deps.shared_state, "get_redis", lambda: None)
    return _RateLimiter()


def _claims(offering: str = OFFERING, sub: str = "u1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, username="alice", course_id=offering, offering_id=offering,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


# --- the limit -------------------------------------------------------------


def test_two_concurrent_streams_are_allowed(limiter):
    """Two, not one: a student who opens a second tab, or reloads before the
    first stream has finished unwinding, is doing something ordinary."""
    assert limiter.acquire_stream("u1")
    assert limiter.acquire_stream("u1")
    assert limiter.active_streams("u1") == 2


def test_a_third_concurrent_stream_is_rejected(limiter):
    limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")

    with pytest.raises(HTTPException) as exc:
        limiter.acquire_stream("u1")
    assert exc.value.status_code == 429


def test_the_rejection_uses_the_existing_typed_error(limiter):
    """No new code. To a student, "too many at once" and "too many per minute"
    are the same instruction — wait, then retry — so the browser's existing
    handler already covers it."""
    limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")

    with pytest.raises(HTTPException) as exc:
        limiter.acquire_stream("u1")
    assert exc.value.detail == ErrorCode.RATE_LIMITED.value


def test_a_refused_stream_does_not_consume_a_slot(limiter):
    """Insert-then-take-back keeps the count-and-insert atomic. If the refusal
    left its own row behind, a student at the limit would be pushed further over
    it by their own retries and could never recover."""
    a = limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")
    with pytest.raises(HTTPException):
        limiter.acquire_stream("u1")

    assert limiter.active_streams("u1") == 2
    limiter.release_stream("u1", a)
    assert limiter.acquire_stream("u1")


def test_the_limit_is_per_student(limiter):
    """One student's streams must not deny another's — that would turn a
    per-student control into an instance-wide outage."""
    limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")

    assert limiter.acquire_stream("u2")
    assert limiter.acquire_stream("u2")
    assert limiter.active_streams("u1") == 2


def test_the_limit_follows_the_configured_number(limiter, monkeypatch):
    monkeypatch.setattr(deps.settings, "max_concurrent_streams", 3)
    for _ in range(3):
        limiter.acquire_stream("u1")
    with pytest.raises(HTTPException):
        limiter.acquire_stream("u1")


# --- release ---------------------------------------------------------------


def test_a_released_slot_can_be_taken_again(limiter):
    a = limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")
    limiter.release_stream("u1", a)

    assert limiter.active_streams("u1") == 1
    assert limiter.acquire_stream("u1")


def test_releasing_twice_is_harmless(limiter):
    """It runs in a `finally` on a streaming path. An exception there would
    replace whatever real error ended the stream with a bookkeeping one."""
    a = limiter.acquire_stream("u1")
    limiter.release_stream("u1", a)
    limiter.release_stream("u1", a)
    assert limiter.active_streams("u1") == 0


def test_releasing_an_unknown_token_is_harmless(limiter):
    limiter.release_stream("u1", "never-issued")
    limiter.release_stream("nobody", "never-issued")


def test_a_leaked_slot_expires_on_its_own(limiter, monkeypatch):
    """The backstop. If a worker is killed mid-generation the `finally` never
    runs, and without an expiry that student is locked out until the process
    restarts — a leak that only ever denies service, with nothing logged."""
    limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")

    # Captured before patching: `deps.time` IS the `time` module, so a lambda
    # calling `time.time()` would call itself.
    later = time.time() + deps._STREAM_LEASE_SECONDS + 1
    monkeypatch.setattr(deps.time, "time", lambda: later)

    assert limiter.active_streams("u1") == 0
    assert limiter.acquire_stream("u1")


def test_released_slots_do_not_accumulate_keys(limiter):
    """One key per student ever seen is a slow leak that nothing surfaces until
    memory does — the same one `_check_local` had to be fixed for."""
    for i in range(50):
        token = limiter.acquire_stream(f"student-{i}")
        limiter.release_stream(f"student-{i}", token)
    assert limiter._streams == {}


# --- the two limits stay separate -----------------------------------------


def test_taking_a_stream_slot_does_not_spend_the_rate_allowance(limiter):
    """They count different things. If acquiring a stream also burned a request,
    the per-minute limit would fall by however many streams a student opened."""
    for _ in range(2):
        limiter.acquire_stream("u1")
    assert limiter._hits.get("u1") in (None, [])


def test_the_rate_limiter_is_not_a_second_object(limiter):
    """One class, one Redis client, one fail-open policy, one typed error. A
    separate concurrency limiter would mean two places to keep those four
    decisions consistent, and they would drift."""
    assert hasattr(deps.rate_limiter, "check")
    assert hasattr(deps.rate_limiter, "acquire_stream")
    assert type(deps.rate_limiter) is _RateLimiter


# --- fail open, like the rate limiter -------------------------------------


def test_a_broken_redis_falls_back_rather_than_denying(monkeypatch):
    """Abuse control, not authorization. Refusing every student because a cache
    is down trades a small abuse risk for a total outage — the same choice
    `check` makes, and the opposite of `boundary/authz.py`."""

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis is gone")

        def zrem(self, *a):
            raise ConnectionError("redis is gone")

    monkeypatch.setattr(deps.shared_state, "get_redis", lambda: Broken())
    seen: list[str] = []
    monkeypatch.setattr(deps.shared_state, "redis_failed",
                        lambda label, exc: seen.append(label))

    limiter = _RateLimiter()
    a = limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")
    with pytest.raises(HTTPException):
        limiter.acquire_stream("u1")   # still enforced, just per-process

    limiter.release_stream("u1", a)
    assert limiter.acquire_stream("u1")
    assert seen, "a redis failure must be reported, not swallowed"


# --- the endpoint wiring ---------------------------------------------------


async def _frames(*, fail: bool = False):
    yield StreamFrame(type=FrameType.TOKEN, text="hello")
    if fail:
        raise RuntimeError("generation blew up")
    yield StreamFrame(type=FrameType.DONE, provider="test")


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_stream_finishes(limiter, monkeypatch):
    from coursemate_service.api import deps, examprep

    # Both names, and the reason is the point of the seam. `examprep` binds the
    # limiter at import for `acquire_stream`; the RELEASE lives in
    # `deps.holding_stream_slot` and resolves `deps.rate_limiter` at call time.
    # One object in production, two names to patch in a test.
    monkeypatch.setattr(examprep, "rate_limiter", limiter)
    monkeypatch.setattr(deps, "rate_limiter", limiter)
    token = limiter.acquire_stream("u1")

    out = [chunk async for chunk in examprep._encode_holding_slot(_frames(), "u1", token)]

    assert len(out) == 2
    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_stream_fails(limiter, monkeypatch):
    """A crash mid-generation must not cost the student a slot permanently."""
    from coursemate_service.api import deps, examprep

    # Both names, and the reason is the point of the seam. `examprep` binds the
    # limiter at import for `acquire_stream`; the RELEASE lives in
    # `deps.holding_stream_slot` and resolves `deps.rate_limiter` at call time.
    # One object in production, two names to patch in a test.
    monkeypatch.setattr(examprep, "rate_limiter", limiter)
    monkeypatch.setattr(deps, "rate_limiter", limiter)
    token = limiter.acquire_stream("u1")

    with pytest.raises(RuntimeError, match="blew up"):
        async for _ in examprep._encode_holding_slot(_frames(fail=True), "u1", token):
            pass

    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_student_disconnects(limiter, monkeypatch):
    """Starlette closes the iterator on disconnect, which arrives as
    `GeneratorExit`. Abandoning a stream is the most common ending of all — a
    student who does not like the question closes the tab."""
    from coursemate_service.api import deps, examprep

    # Both names, and the reason is the point of the seam. `examprep` binds the
    # limiter at import for `acquire_stream`; the RELEASE lives in
    # `deps.holding_stream_slot` and resolves `deps.rate_limiter` at call time.
    # One object in production, two names to patch in a test.
    monkeypatch.setattr(examprep, "rate_limiter", limiter)
    monkeypatch.setattr(deps, "rate_limiter", limiter)
    token = limiter.acquire_stream("u1")

    stream = examprep._encode_holding_slot(_frames(), "u1", token)
    await stream.__anext__()          # one frame, then walk away
    await stream.aclose()

    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_the_slot_is_taken_before_the_generator_runs(monkeypatch):
    """Acquiring after generation started would count streams that are already
    consuming the thing the limit protects, and the third student would be
    refused only once their model call was in flight."""
    from coursemate_service.api import examprep

    order: list[str] = []

    class Recording(_RateLimiter):
        def acquire_stream(self, student_id):
            order.append("acquire")
            return "t"

    class Gen:
        def stream(self, *a, **kw):
            order.append("generate")
            return _frames()

    monkeypatch.setattr(deps.shared_state, "get_redis", lambda: None)
    monkeypatch.setattr(examprep, "rate_limiter", Recording())
    import coursemate_service.ai.quiz_generator as qg

    monkeypatch.setattr(qg, "generator", Gen())

    from coursemate_contracts.examprep import PracticeRequest

    await examprep.practice_stream(PracticeRequest(clo_id="CLO-1"), _claims())
    assert order == ["acquire", "generate"]


@pytest.mark.asyncio
async def test_a_third_stream_never_reaches_the_generator(monkeypatch, limiter):
    """The point of refusing early: no model call, no cost, no provider slot."""
    from coursemate_service.api import examprep

    called: list[int] = []

    class Gen:
        def stream(self, *a, **kw):
            called.append(1)
            return _frames()

    monkeypatch.setattr(examprep, "rate_limiter", limiter)
    import coursemate_service.ai.quiz_generator as qg

    monkeypatch.setattr(qg, "generator", Gen())

    from coursemate_contracts.examprep import PracticeRequest

    req = PracticeRequest(clo_id="CLO-1")
    await examprep.practice_stream(req, _claims())
    await examprep.practice_stream(req, _claims())
    with pytest.raises(HTTPException) as exc:
        await examprep.practice_stream(req, _claims())

    assert exc.value.status_code == 429
    assert len(called) == 2, "the refused request must not have generated anything"


# --- scope -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_slots_are_keyed_on_the_verified_subject_not_the_offering(monkeypatch, limiter):
    """The same student in two courses shares one budget, because the provider
    concurrency they occupy is one pool. Keying on the offering would let a
    student enrolled in five courses hold ten streams."""
    from coursemate_contracts.examprep import PracticeRequest
    from coursemate_service.api import examprep

    class Gen:
        def stream(self, *a, **kw):
            return _frames()

    monkeypatch.setattr(examprep, "rate_limiter", limiter)
    import coursemate_service.ai.quiz_generator as qg

    monkeypatch.setattr(qg, "generator", Gen())

    req = PracticeRequest(clo_id="CLO-1")
    await examprep.practice_stream(req, _claims(OFFERING, sub="u1"))
    await examprep.practice_stream(req, _claims(OTHER, sub="u1"))

    with pytest.raises(HTTPException):
        await examprep.practice_stream(req, _claims(OFFERING, sub="u1"))


def test_one_student_cannot_release_another_students_slot(limiter):
    """Tokens are per-acquisition and the key is the student id, so a token
    cannot be used across subjects even if it leaked."""
    a = limiter.acquire_stream("u1")
    limiter.acquire_stream("u1")

    limiter.release_stream("u2", a)          # wrong student, no effect
    assert limiter.active_streams("u1") == 2
    with pytest.raises(HTTPException):
        limiter.acquire_stream("u1")
