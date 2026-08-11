"""A fallback provider must not serve healthy traffic, and a healthy primary
must not be reported as degraded.

**Both halves were wrong, and both were invisible.**

`fallback_model` was registered as a second deployment named `strong`. Sharing a
`model_name` does not make a priority chain — the Router load-balances across
them — so the secondary vendor served roughly half of every student's answers
while both providers were healthy. Nothing fails in that state: two working
providers both return answers.

The DEGRADED frame then compounded it. It fired on
`provider_used not in settings.strong_model`, a substring test against the
configured string. Providers return versioned ids, so a healthy
`claude-opus-5-20260514` against a configured `anthropic/claude-opus-5` is not a
substring and every answer would have carried a degradation warning.

Neither could be caught by review of this repo alone — they only appear when a
real Router runs. So the load-balancing test below builds one, with mocked
completions and no network.
"""

from __future__ import annotations

import time
from collections import Counter

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, Citation, FrameType

from coursemate_service.ai import client
from coursemate_service.ai import pipeline as pl
from coursemate_service.ai.context import ContextChunk, ContextResult


# --- deployment naming ----------------------------------------------------


def _configure(monkeypatch, *, strong, cheap, fallback):
    client.reset_router()
    monkeypatch.setattr(client.settings, "strong_model", strong)
    monkeypatch.setattr(client.settings, "cheap_model", cheap)
    monkeypatch.setattr(client.settings, "fallback_model", fallback)
    monkeypatch.setattr(client.settings, "model_api_key", "sk-mock")
    monkeypatch.setattr(client.settings, "fallback_api_key", "sk-mock-2")
    monkeypatch.setattr(client.settings, "redis_url", "")


def test_the_fallback_is_not_registered_as_a_second_strong(monkeypatch):
    """The bug itself, at the point it was introduced."""
    _configure(monkeypatch, strong="openai/a", cheap="openai/b", fallback="anthropic/c")

    names = [m["model_name"] for m in client.build_model_list()]
    assert names.count("strong") == 1, f"strong is duplicated: {names}"
    assert "fallback" in names
    client.reset_router()


def test_a_fallback_only_configuration_still_answers(monkeypatch):
    """With no strong model there is nothing to fall back FROM, so the fallback
    is the primary. Otherwise the pipeline asks the Router for `strong` and gets
    a name that does not resolve."""
    _configure(monkeypatch, strong="", cheap="", fallback="anthropic/c")

    assert [m["model_name"] for m in client.build_model_list()] == ["strong"]
    client.reset_router()


def test_the_chain_puts_a_different_vendor_before_the_cheap_model(monkeypatch):
    """`cheap` shares the primary's vendor, so it shares the primary's outage."""
    _configure(monkeypatch, strong="openai/a", cheap="openai/b", fallback="anthropic/c")

    chain = client.build_fallback_chain(client.build_model_list())
    assert chain == [{"strong": ["fallback", "cheap"]}]
    client.reset_router()


def test_the_chain_never_names_an_unregistered_deployment(monkeypatch):
    """Routing to a name that was never registered turns a recoverable provider
    error into an unrecoverable one."""
    _configure(monkeypatch, strong="openai/a", cheap="", fallback=None)

    assert client.build_fallback_chain(client.build_model_list()) == []
    client.reset_router()


# --- what the Router actually does ----------------------------------------


@pytest.mark.asyncio
async def test_healthy_traffic_never_reaches_the_fallback(monkeypatch):
    """The measurement that found this.

    Against the real Router, the old shape sent 20 of 40 calls to the fallback
    while both providers were healthy. Every completion here is mocked, so this
    exercises real routing with no network and no key.

    **Asserted on the concrete model, not the deployment name.** Under the old
    shape both deployments were *called* `strong`, so a name-based assertion
    passes while the fallback is serving half the traffic — the test would have
    agreed with the bug.
    """
    _configure(monkeypatch, strong="openai/primary", cheap="openai/cheap",
               fallback="anthropic/secondary")
    router = client.get_router()

    served = Counter()
    for _ in range(20):
        response = await router.acompletion(
            model="strong",
            messages=[{"role": "user", "content": "hi"}],
            mock_response="ok",
        )
        served[response.model] += 1
        assert client.deployment_of(response) == "strong"

    assert set(served) == {"primary"}, f"fallback served healthy traffic: {dict(served)}"
    client.reset_router()


@pytest.mark.asyncio
async def test_the_answering_deployment_is_identifiable_while_streaming(monkeypatch):
    """The pipeline only ever sees stream chunks, never a whole response, so the
    identity has to survive streaming or the DEGRADED frame has nothing to read."""
    _configure(monkeypatch, strong="openai/primary", cheap="", fallback="anthropic/secondary")
    router = client.get_router()

    response = await router.acompletion(
        model="strong",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        mock_response="hello there",
    )
    names = {client.deployment_of(part) async for part in response}

    assert names == {"strong"}, f"deployment unidentifiable mid-stream: {names}"
    client.reset_router()


# --- the DEGRADED frame ----------------------------------------------------


def _claims() -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="u1", course_id="c", offering_id="c", roles=["student"],
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


def _grounded():
    class _P:
        async def fetch(self, question, claims):  # noqa: ARG002
            return ContextResult(
                chunks=[ContextChunk(
                    text="Locks are acquired in a fixed order.",
                    citation=Citation(usage_key="u1", display_name="Locks"), score=0.9)],
                top_score=0.9,
            )
    return _P()


def _router_answering_as(model_name: str | None):
    """A stub router whose chunk resolves to `model_name`, or to nothing."""
    class _Chunk:
        def __init__(self):
            self.choices = [type("C", (), {
                "delta": type("D", (), {"content": "Locks are acquired in order."})(),
                "finish_reason": "stop",
            })()]
            self.model = "some-vendor-model-20260514"

    async def _stream():
        yield _Chunk()

    class _Router:
        async def acompletion(self, **kw):  # noqa: ARG002
            return _stream()

    return _Router()


async def _frames(monkeypatch, resolves_to):
    monkeypatch.setattr(pl, "get_router", lambda: _router_answering_as(resolves_to))
    monkeypatch.setattr(pl, "deployment_of", lambda part: resolves_to)  # noqa: ARG005
    return [f async for f in pl.AnswerPipeline(_grounded()).stream(
        ChatRequest(question="q"), _claims())]


@pytest.mark.asyncio
async def test_a_healthy_primary_is_not_reported_degraded(monkeypatch):
    """The bug: the model string the vendor returns is versioned, so a substring
    test against the configured name flagged every healthy answer."""
    frames = await _frames(monkeypatch, "strong")
    assert [f for f in frames if f.type == FrameType.DEGRADED] == []


@pytest.mark.asyncio
async def test_a_fallback_answer_is_reported_degraded(monkeypatch):
    """The control arm. A check that never fires is the defect this replaced,
    not an improvement on it."""
    frames = await _frames(monkeypatch, "fallback")
    degraded = [f for f in frames if f.type == FrameType.DEGRADED]
    assert len(degraded) == 1
    assert degraded[0].provider == "some-vendor-model-20260514"


@pytest.mark.asyncio
async def test_an_unidentifiable_deployment_is_not_called_degraded(monkeypatch):
    """Unknown is not degraded. Warning a student about a failure we invented by
    failing to look something up is worse than staying quiet."""
    frames = await _frames(monkeypatch, None)
    assert [f for f in frames if f.type == FrameType.DEGRADED] == []
