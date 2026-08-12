"""`/chat` holds a concurrency slot, exactly as `/practice/stream` does.

It did not until 2026-08-12, and the asymmetry was an oversight rather than a
decision: the slot was added with the practice stream and the commit that added
it is titled for that route. Chat is the longer generation of the two — ~55 s
measured against the local model — so it is the one that most needs bounding.

The gap this closes is specific. Rate limiting caps how *often* a student starts
a stream; nothing capped how many they hold open at once, so one student could
occupy up to `student_requests_per_minute` provider slots simultaneously while
every other student queued behind them.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, FrameType, StreamFrame
from coursemate_service import shared_state
from coursemate_service.api import chat as chat_api
from coursemate_service.api import deps
from coursemate_service.api.deps import _RateLimiter
from fastapi import HTTPException

OFFERING = "course-v1:X+Y+Z"


@pytest.fixture
def limiter(monkeypatch):
    """A private limiter on the per-process path, patched into BOTH names.

    `chat` binds the object at import for `acquire_stream`; the release lives in
    `deps.holding_stream_slot` and resolves `deps.rate_limiter` at call time. One
    object in production, two names to patch here.
    """
    shared_state.reset_for_tests()
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    lim = _RateLimiter()
    monkeypatch.setattr(chat_api, "rate_limiter", lim)
    monkeypatch.setattr(deps, "rate_limiter", lim)
    return lim


def _claims(sub="u1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, course_id=OFFERING, offering_id=OFFERING, roles=["student"],
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


async def _frames(*, fail: bool = False):
    yield StreamFrame(type=FrameType.TOKEN, text="hello")
    if fail:
        raise RuntimeError("generation blew up")
    yield StreamFrame(type=FrameType.DONE, provider="test")


# --- the route takes a slot -------------------------------------------------


@pytest.mark.asyncio
async def test_chat_takes_a_slot(limiter, monkeypatch):
    monkeypatch.setattr(chat_api, "_encode", lambda request, claims: _frames())

    await chat_api.chat(ChatRequest(question="hi"), _claims())

    assert limiter.active_streams("u1") == 1, "/chat did not take a concurrency slot"


@pytest.mark.asyncio
async def test_a_third_concurrent_chat_is_refused(limiter, monkeypatch):
    """`max_concurrent_streams` is 2 — a second tab is ordinary, a third is not."""
    monkeypatch.setattr(chat_api, "_encode", lambda request, claims: _frames())

    await chat_api.chat(ChatRequest(question="one"), _claims())
    await chat_api.chat(ChatRequest(question="two"), _claims())

    with pytest.raises(HTTPException) as exc:
        await chat_api.chat(ChatRequest(question="three"), _claims())

    assert exc.value.status_code == 429
    assert exc.value.detail == "rate_limited"


@pytest.mark.asyncio
async def test_the_limit_is_per_student_not_global(limiter, monkeypatch):
    """Two students filling their own slots must not refuse a third student."""
    monkeypatch.setattr(chat_api, "_encode", lambda request, claims: _frames())

    await chat_api.chat(ChatRequest(question="q"), _claims("u1"))
    await chat_api.chat(ChatRequest(question="q"), _claims("u1"))
    await chat_api.chat(ChatRequest(question="q"), _claims("u2"))

    assert limiter.active_streams("u1") == 2
    assert limiter.active_streams("u2") == 1


@pytest.mark.asyncio
async def test_the_slot_is_taken_before_the_generator_runs(limiter, monkeypatch):
    """A slot taken after generation started would count streams already
    consuming the thing it protects, and the third would be refused only once its
    model call was in flight."""
    order: list[str] = []

    class Recording(_RateLimiter):
        def acquire_stream(self, student_id):
            order.append("acquire")
            return "t"

    monkeypatch.setattr(chat_api, "rate_limiter", Recording())

    def _encode(request, claims):
        order.append("generate")
        return _frames()

    monkeypatch.setattr(chat_api, "_encode", _encode)

    await chat_api.chat(ChatRequest(question="hi"), _claims())

    assert order and order[0] == "acquire", f"generator ran first: {order}"


@pytest.mark.asyncio
async def test_a_refused_chat_never_reaches_the_pipeline(limiter, monkeypatch):
    """The point of refusing early: no retrieval, no provider call, no spend."""
    started: list[str] = []

    def _encode(request, claims):
        started.append("x")
        return _frames()

    monkeypatch.setattr(chat_api, "_encode", _encode)

    await chat_api.chat(ChatRequest(question="one"), _claims())
    await chat_api.chat(ChatRequest(question="two"), _claims())
    with pytest.raises(HTTPException):
        await chat_api.chat(ChatRequest(question="three"), _claims())

    # StreamingResponse does not consume the iterator until it is sent, so the
    # count here is of generators CREATED, and the refused one must not be.
    assert len(started) == 2


# --- and gives it back ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_slot_is_still_held_while_the_stream_is_running(limiter):
    """**The property the `finally` exists for**, and the one the other release
    tests cannot see.

    Releasing eagerly — at the top of the wrapper, or in the endpoint — also ends
    with zero held slots, so every "it was released" assertion passes while the
    limit protects nothing: the slot is free for the entire generation it was
    supposed to be counting. Found by deliberately moving the release out of the
    `finally` and watching this file stay green.
    """
    token = limiter.acquire_stream("u1")
    stream = deps.holding_stream_slot(_encoded(), "u1", token)

    assert await stream.__anext__() == "a"
    assert limiter.active_streams("u1") == 1, "the slot was freed mid-stream"

    assert await stream.__anext__() == "b"
    assert limiter.active_streams("u1") == 1, "the slot was freed before the end"

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_a_second_student_is_refused_while_the_first_stream_runs(limiter, monkeypatch):
    """The same property through the route: two streams in flight fill both
    slots, and they stay filled until those streams end."""
    monkeypatch.setattr(chat_api, "_encode", lambda request, claims: _frames())

    r1 = await chat_api.chat(ChatRequest(question="one"), _claims())
    r2 = await chat_api.chat(ChatRequest(question="two"), _claims())

    # Start both without finishing either.
    await r1.body_iterator.__anext__()
    await r2.body_iterator.__anext__()

    assert limiter.active_streams("u1") == 2
    with pytest.raises(HTTPException) as exc:
        await chat_api.chat(ChatRequest(question="three"), _claims())
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_stream_finishes(limiter):
    token = limiter.acquire_stream("u1")

    out = [c async for c in deps.holding_stream_slot(_encoded(), "u1", token)]

    assert out == ["a", "b"]
    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_stream_fails(limiter):
    """A crash mid-generation must not cost the student a slot permanently."""
    token = limiter.acquire_stream("u1")

    with pytest.raises(RuntimeError, match="blew up"):
        async for _ in deps.holding_stream_slot(_encoded(fail=True), "u1", token):
            pass

    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_the_slot_is_released_when_the_student_disconnects(limiter):
    """Starlette closes the iterator on disconnect, arriving as `GeneratorExit`.
    Abandoning a stream is the most common ending of all — a student who does not
    like the answer closes the tab."""
    token = limiter.acquire_stream("u1")

    stream = deps.holding_stream_slot(_encoded(), "u1", token)
    await stream.__anext__()
    await stream.aclose()

    assert limiter.active_streams("u1") == 0


@pytest.mark.asyncio
async def test_a_released_chat_slot_can_be_reused(limiter, monkeypatch):
    """End to end: fill both slots, drain one, and the next request is allowed
    again — the limit is on concurrency, not on a daily count."""
    monkeypatch.setattr(chat_api, "_encode", lambda request, claims: _frames())

    r1 = await chat_api.chat(ChatRequest(question="one"), _claims())
    await chat_api.chat(ChatRequest(question="two"), _claims())
    with pytest.raises(HTTPException):
        await chat_api.chat(ChatRequest(question="three"), _claims())

    async for _ in r1.body_iterator:      # drain the first stream to completion
        pass

    assert limiter.active_streams("u1") == 1
    await chat_api.chat(ChatRequest(question="four"), _claims())


async def _encoded(*, fail: bool = False):
    yield "a"
    if fail:
        raise RuntimeError("blew up")
    yield "b"


# --- the shared helper is shared -------------------------------------------


def test_there_is_one_release_implementation():
    """`examprep._encode_holding_slot` must delegate rather than keep its own
    `finally`. Two copies of "when is a slot safe to give back" drift, and the
    drift is invisible: each route looks correct in isolation."""
    import ast
    import inspect
    import textwrap

    from coursemate_service.api import examprep

    fn = ast.parse(
        textwrap.dedent(inspect.getsource(examprep._encode_holding_slot))
    ).body[0]
    # Docstring out: it *describes* the finally that used to be here, and
    # matching prose instead of code is how a guard like this passes for the
    # wrong reason.
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))

    assert "holding_stream_slot" in code
    assert "try" not in code and "finally" not in code, (
        "examprep grew a second release path"
    )
    assert "release_stream" not in code
