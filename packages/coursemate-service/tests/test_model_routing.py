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


def _configure(monkeypatch, *, strong, cheap, fallback, cheap_key=None, cheap_base=None):
    client.reset_router()
    monkeypatch.setattr(client.settings, "strong_model", strong)
    monkeypatch.setattr(client.settings, "cheap_model", cheap)
    monkeypatch.setattr(client.settings, "fallback_model", fallback)
    monkeypatch.setattr(client.settings, "model_api_key", "sk-mock")
    monkeypatch.setattr(client.settings, "model_api_base", None)
    monkeypatch.setattr(client.settings, "cheap_api_key", cheap_key)
    monkeypatch.setattr(client.settings, "cheap_api_base", cheap_base)
    monkeypatch.setattr(client.settings, "fallback_api_key", "sk-mock-2")
    monkeypatch.setattr(client.settings, "redis_url", "")


def _params(models, name) -> dict:
    return next(m["litellm_params"] for m in models if m["model_name"] == name)


# --- one credential pair per deployment (2026-08-14) ----------------------
#
# `strong` and `cheap` shared `model_api_key`/`model_api_base` until ADR-0001
# split the tiers across two vendors. One base URL cannot address two providers,
# so the documented recipe for reaching a local model — set MODEL_API_BASE —
# also pointed the HOSTED primary at the local server. Nothing failed loudly;
# the primary just stopped working, for a reason no setting named.


def test_the_cheap_tier_can_have_its_own_base_url(monkeypatch):
    """The topology this exists for: hosted primary, local floor."""
    _configure(
        monkeypatch,
        strong="openrouter/meta-llama/llama-3.3-70b-instruct",
        cheap="ollama_chat/qwen2.5:7b",
        fallback="",
        cheap_base="http://172.18.0.1:11435",
    )
    models = client.build_model_list()

    assert _params(models, "cheap")["api_base"] == "http://172.18.0.1:11435"
    assert "api_base" not in _params(models, "strong"), (
        "the hosted primary was given the local model's base URL — this is the "
        "exact failure the split prevents"
    )
    client.reset_router()


def test_the_hosted_key_is_not_handed_to_the_local_model(monkeypatch):
    _configure(
        monkeypatch,
        strong="openrouter/x", cheap="ollama_chat/y", fallback="",
        cheap_key="sk-local", cheap_base="http://localhost:11434",
    )
    models = client.build_model_list()

    assert _params(models, "strong")["api_key"] == "sk-mock"
    assert _params(models, "cheap")["api_key"] == "sk-local"
    client.reset_router()


def test_one_vendor_still_configures_one_pair(monkeypatch):
    """The fallback that keeps this from being a breaking change. A deployment
    with both tiers on one vendor sets `model_*` and nothing else, exactly as
    before."""
    _configure(monkeypatch, strong="anthropic/a", cheap="anthropic/b", fallback="")
    monkeypatch.setattr(client.settings, "model_api_base", "https://gw.example/v1")
    models = client.build_model_list()

    for name in ("strong", "cheap"):
        assert _params(models, name)["api_key"] == "sk-mock"
        assert _params(models, name)["api_base"] == "https://gw.example/v1"
    client.reset_router()


def test_an_empty_cheap_key_falls_back_rather_than_configuring_nothing(monkeypatch):
    """The Tutor plugin renders an unset variable as `""`, not as absent. Read
    as "configured, to nothing", that would hand `cheap` an empty credential the
    day the template ships the default — which is why the code uses `or`."""
    _configure(
        monkeypatch, strong="anthropic/a", cheap="anthropic/b", fallback="",
        cheap_key="", cheap_base="",
    )
    models = client.build_model_list()

    assert _params(models, "cheap")["api_key"] == "sk-mock"
    client.reset_router()


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


# --- generation is not chat -----------------------------------------------
#
# §9.0 lets a generated practice question reach a student with no instructor
# gate BECAUSE the output is measured. The Feature B rubric scored the strong
# model. Anything else answering means unmeasured output shipped under a
# measurement it never earned.
#
# The exclusion used to be justified twice over — §9.0, and "cheap shares the
# primary's vendor so it fails with it anyway". The second half went false on
# 2026-08-14 when `strong` moved to a hosted provider and `cheap` became the
# local model. They now share neither vendor nor machine, so on availability
# grounds `cheap` is the BEST failover in the list. These tests exist because
# that makes the exclusion a deliberate trade rather than a free one, and a
# free-looking rule is the kind someone quietly relaxes.


def test_generation_never_falls_back_to_the_local_floor(monkeypatch):
    """The topology that broke the old rationale: hosted primary, hosted
    fallback, LOCAL cheap. `cheap` survives every hosted outage and still must
    not generate — it is unmeasured."""
    _configure(
        monkeypatch,
        strong="openrouter/meta-llama/llama-3.3-70b-instruct",
        cheap="ollama_chat/qwen2.5:7b",
        fallback="gemini/gemini-2.0-flash",
    )
    model_list = client.build_model_list()

    generation = client.build_generation_fallback_chain(model_list)
    assert generation == [{"strong": ["fallback"]}]

    # Chat keeps the floor — a cited answer from a weaker model still helps, and
    # DEGRADED says where it came from. The two chains must differ.
    chat = client.build_fallback_chain(model_list)
    assert chat == [{"strong": ["fallback", "cheap"]}]
    assert generation != chat, "generation inherited chat's chain"
    client.reset_router()


def test_generation_has_no_fallback_at_all_when_only_the_local_floor_remains(monkeypatch):
    """No hosted fallback configured, local `cheap` available and healthy.

    The chain must be EMPTY, so a `strong` outage becomes UNAVAILABLE rather
    than a silently-local question. No question is better than an unmeasured one
    presented as measured. This is the live topology as of 2026-08-14.
    """
    _configure(
        monkeypatch,
        strong="openrouter/meta-llama/llama-3.3-70b-instruct",
        cheap="ollama_chat/qwen2.5:7b",
        fallback=None,
    )
    model_list = client.build_model_list()

    assert client.build_generation_fallback_chain(model_list) == []
    # …while chat still degrades to the floor rather than failing.
    assert client.build_fallback_chain(model_list) == [{"strong": ["cheap"]}]
    client.reset_router()


def test_the_local_floor_is_registered_but_simply_not_named_for_generation(monkeypatch):
    """Distinguishes "excluded" from "absent".

    If `cheap` were merely unregistered the chain would also be short, and the
    test above would pass for the wrong reason — hiding a config bug that
    removed the floor from chat too.
    """
    _configure(
        monkeypatch,
        strong="openrouter/x", cheap="ollama_chat/qwen2.5:7b", fallback="gemini/y",
    )
    model_list = client.build_model_list()

    assert "cheap" in {m["model_name"] for m in model_list}
    assert "cheap" not in client.build_generation_fallback_chain(model_list)[0]["strong"]
    client.reset_router()


def test_generation_falls_back_only_to_a_different_vendor_never_a_second_local(monkeypatch):
    """Two local deployments, no hosted fallback: still nothing to fall back to.

    Guards the reading that "cheap is a different vendor now, so let it
    generate" — the exclusion is about measurement, not vendor identity.
    """
    _configure(
        monkeypatch,
        strong="ollama_chat/qwen2.5:7b", cheap="ollama_chat/llama3.2:3b", fallback=None,
    )
    assert client.build_generation_fallback_chain(client.build_model_list()) == []
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
        async def fetch(self, question, claims):
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
        async def acompletion(self, **kw):
            return _stream()

    return _Router()


async def _frames(monkeypatch, resolves_to):
    monkeypatch.setattr(pl, "get_router", lambda: _router_answering_as(resolves_to))
    monkeypatch.setattr(pl, "deployment_of", lambda part: resolves_to)
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
