"""The running instance can now say something about itself.

Every quality claim in this project is measured — offline, by `eval/`, against a
gold set. The *running* service measured nothing, so "what is the abstention rate
this week", "is the cache ever hitting", "how often is the provider failing" had
no answer short of re-running the harness. Phase D2 spent hours diagnosing by
hand what two of these counters would have shown.

These tests exist mostly to stop the counters becoming decorative: a metric that
is exported but never incremented reads exactly like a quiet system.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, Citation
from coursemate_service import metrics, shared_state
from coursemate_service.ai import client as ai_client
from coursemate_service.ai import pipeline as pl
from coursemate_service.ai.context import ContextChunk, ContextResult
from coursemate_service.main import app
from fastapi.testclient import TestClient

client = TestClient(app)
OFFERING = "course-v1:X+Y+Z"
METRICS = "/coursemate/metrics"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset_for_tests()
    shared_state.reset_for_tests()
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    yield
    metrics.reset_for_tests()


# --- the counter primitive --------------------------------------------------


def test_counters_start_at_zero_rather_than_absent():
    """A declared-but-never-incremented counter must render as 0. Creating keys
    on first use makes "nothing happened" indistinguishable from "the code path
    was never wired"."""
    body = metrics.render()
    for name in metrics._COUNTERS:
        assert f"coursemate_{name} 0" in body


def test_increment_adds_up():
    metrics.increment("chat_requests_total")
    metrics.increment("chat_requests_total", 4)
    assert metrics.snapshot()["chat_requests_total"] == 5


def test_an_unknown_counter_raises_rather_than_appearing():
    """A typo must not silently create a metric nobody reads — that is how a
    dashboard ends up measuring nothing."""
    with pytest.raises(KeyError):
        metrics.increment("chat_requsts_total")


def test_the_render_is_valid_prometheus_text():
    body = metrics.render()
    for name, help_text in metrics._COUNTERS.items():
        assert f"# HELP coursemate_{name} {help_text}" in body
        assert f"# TYPE coursemate_{name} counter" in body
    assert body.endswith("\n")


# --- the endpoint -----------------------------------------------------------


def test_metrics_requires_the_service_credential():
    """Aggregates are not student data, but `/coursemate/*` is reachable
    same-origin by any logged-in browser and request volume is a signal about an
    institution."""
    assert client.get(METRICS).status_code == 401


def test_metrics_serves_with_the_credential():
    from coursemate_service.config import settings

    r = client.get(METRICS, headers={"Authorization": f"Bearer {settings.service_credential}"})
    assert r.status_code == 200
    assert "coursemate_chat_requests_total" in r.text


def test_metrics_is_not_published_in_the_api_spec():
    """An operational surface, not something to integrate against."""
    assert METRICS not in app.openapi()["paths"]


# --- the counters are actually wired ---------------------------------------


def _claims(sub="u1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, course_id=OFFERING, offering_id=OFFERING, roles=["student"],
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


class _Ctx:
    def __init__(self, score=0.9, version="idx-v1"):
        self.score, self.version = score, version

    async def fetch(self, question, claims):
        return ContextResult(
            chunks=[ContextChunk(text="A cohort is a group.",
                                 citation=Citation(usage_key="u1", display_name="Cohorts"),
                                 score=self.score)],
            top_score=self.score, index_version=self.version,
        )


class _Chunk:
    def __init__(self, text, finish=None):
        self.choices = [type("C", (), {
            "delta": type("D", (), {"content": text})(), "finish_reason": finish})()]
        self.model = "test-model"


def _router(calls, chunks):
    async def _stream():
        for c in chunks:
            yield c

    class _R:
        async def acompletion(self, **kw):
            calls.append(kw)
            return _stream()

    return _R()


def _install(monkeypatch, chunks=None):
    calls: list = []
    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router",
                        lambda: _router(calls, chunks or [_Chunk("an answer", "stop")]))
    monkeypatch.setattr(pl, "deployment_of", lambda part: pl.PRIMARY_DEPLOYMENT)
    monkeypatch.setattr(pl.settings, "require_grounding", True)
    monkeypatch.setattr(pl.settings, "student_daily_token_budget", 0)
    return calls


async def _ask(ctx, request, claims):
    return [f async for f in pl.AnswerPipeline(ctx).stream(request, claims)]


@pytest.mark.asyncio
async def test_a_request_is_counted(monkeypatch):
    _install(monkeypatch)
    await _ask(_Ctx(), ChatRequest(question="hi"), _claims())
    assert metrics.snapshot()["chat_requests_total"] == 1


@pytest.mark.asyncio
async def test_an_abstention_is_counted(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(pl.settings, "confidence_threshold", 0.99)

    frames = await _ask(_Ctx(score=0.1), ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code.value == "abstained"
    assert metrics.snapshot()["abstentions_total"] == 1


@pytest.mark.asyncio
async def test_preparing_is_not_counted_as_an_abstention(monkeypatch):
    """Different states, and the metric must not blur them: 'not covered' is a
    settled answer, 'still being prepared' is a course that is not ready."""
    _install(monkeypatch)

    class _NoIndex:
        async def fetch(self, question, claims):
            return ContextResult(chunks=[], top_score=0.0, index_missing=True)

    await _ask(_NoIndex(), ChatRequest(question="hi"), _claims())
    assert metrics.snapshot()["abstentions_total"] == 0


@pytest.mark.asyncio
async def test_a_budget_refusal_is_counted(monkeypatch):
    from coursemate_service import budget

    _install(monkeypatch)
    monkeypatch.setattr(pl.settings, "student_daily_token_budget", 100)
    budget.ledger.reset_for_tests()
    budget.ledger.record(OFFERING, "u1", 500)

    frames = await _ask(_Ctx(), ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code.value == "budget_exceeded"
    assert metrics.snapshot()["budget_refusals_total"] == 1
    budget.ledger.reset_for_tests()


@pytest.mark.asyncio
async def test_a_provider_failure_is_counted(monkeypatch):
    async def _boom():
        raise RuntimeError("provider down")
        yield  # pragma: no cover

    class _R:
        async def acompletion(self, **kw):
            return _boom()

    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router", lambda: _R())
    monkeypatch.setattr(pl.settings, "require_grounding", True)
    monkeypatch.setattr(pl.settings, "student_daily_token_budget", 0)

    frames = await _ask(_Ctx(), ChatRequest(question="hi"), _claims())

    assert frames[-1].error_code.value == "unavailable"
    assert metrics.snapshot()["provider_failures_total"] == 1


@pytest.mark.asyncio
async def test_cache_miss_then_hit_are_counted(monkeypatch):
    """The pair that D2 could not answer without reading Redis by hand."""
    class _FakeRedis:
        def __init__(self):
            self.kv = {}

        def get(self, k):
            return self.kv.get(k)

        def setex(self, k, ttl, v):
            self.kv[k] = v

    # ONE instance, not one per call — `lambda: _FakeRedis()` hands out a fresh
    # empty store to every caller, so nothing written by the first request is
    # there for the second and both count as misses.
    fake = _FakeRedis()
    monkeypatch.setattr(shared_state, "get_redis", lambda: fake)
    calls = _install(monkeypatch)
    ctx = _Ctx()
    request = ChatRequest(question="What is a cohort?")

    await _ask(ctx, request, _claims())
    await _ask(ctx, request, _claims())

    snap = metrics.snapshot()
    assert snap["cache_misses_total"] == 1
    assert snap["cache_hits_total"] == 1
    assert len(calls) == 1, "a cache hit still called the provider"
