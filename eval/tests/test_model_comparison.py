"""The comparison harness is a measuring instrument, so it gets measured.

Four properties decide whether a report from this thing means anything. Each one
fails silently if broken — the report still renders, still looks like a
comparison, and is wrong:

  1. **Pinning.** Every call must name its own deployment. If the harness asked
     for "strong" and let the fallback chain resolve, the table would report
     whatever answered under the name of something else.
  2. **Shared context.** Every deployment must see byte-identical messages. Fetch
     per deployment and a retrieval difference reads as a model difference.
  3. **Errors are data.** One dead provider must not end a run that exists to
     compare providers — that would leave the report silent about exactly the
     deployment worth reporting on.
  4. **Missing usage renders `—`.** `ollama_chat` reports no usage on stream
     chunks. Printing `0` there would claim the model spent no tokens.

The harness is driven against a scripted router rather than a real one: these are
properties of the harness, and a real provider would make them slow, flaky and
no better observed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_model_comparison import (
    UNKNOWN,
    ComparisonReport,
    QuestionComparison,
    compare_question,
    deployments_from,
    find_aliases,
    render_markdown,
    run_deployment,
    to_json,
)

MESSAGES = [
    {"role": "system", "content": "Answer only from the course material."},
    {"role": "user", "content": "What is a cohort?"},
]
CONTEXT = ["A cohort is a group of learners.", "Cohorts can see different content."]


# --- scripted router -------------------------------------------------------


def _chunk(text: str = "", usage: dict | None = None):
    """One stream chunk. Usage arrives on a chunk with EMPTY choices, which is
    the shape a real provider sends and the shape a naive reader skips."""
    choices = (
        [SimpleNamespace(delta=SimpleNamespace(content=text))] if text else []
    )
    return SimpleNamespace(
        choices=choices,
        usage=SimpleNamespace(**usage) if usage else None,
    )


class FakeRouter:
    """Records every call and replays a scripted stream per deployment."""

    def __init__(self, scripts: dict[str, list], errors: dict[str, Exception] | None = None):
        self.scripts = scripts
        self.errors = errors or {}
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["model"]
        if name in self.errors:
            raise self.errors[name]

        async def stream():
            for chunk in self.scripts.get(name, []):
                yield chunk

        return stream()


def _run(coro):
    return asyncio.run(coro)


# --- 1. pinning ------------------------------------------------------------


def test_each_call_pins_its_own_deployment():
    router = FakeRouter({"strong": [_chunk("a")], "cheap": [_chunk("b")],
                         "fallback": [_chunk("c")]})
    deployments = [("strong", "groq/x"), ("cheap", "ollama/y"), ("fallback", "openrouter/z")]

    _run(compare_question(router, deployments, MESSAGES, CONTEXT))

    assert [c["model"] for c in router.calls] == ["strong", "cheap", "fallback"]


def test_it_never_asks_for_the_primary_on_behalf_of_another_deployment():
    """The failure this exists to prevent: pinning "strong" for every row and
    letting the fallback chain decide who actually answers."""
    router = FakeRouter({"cheap": [_chunk("b")]})
    _run(compare_question(router, [("cheap", "ollama/y")], MESSAGES, CONTEXT))
    assert router.calls[0]["model"] == "cheap"


def test_fallbacks_are_disabled_on_every_comparison_call():
    """Pinning the NAME is not enough, and this was found on the live stack.

    With the local provider unreachable, a call pinned to `strong` reported
    `Received Model Group=strong / Available Model Group Fallbacks=['cheap']`
    and the Router went on to try `cheap`. Had `cheap` been healthy it would
    have answered and its answer would have landed in the `strong` row — a
    comparison silently comparing one model with itself.
    """
    router = FakeRouter({"strong": [_chunk("a")], "cheap": [_chunk("b")]})
    _run(compare_question(
        router, [("strong", "groq/x"), ("cheap", "ollama/y")], MESSAGES, CONTEXT))

    for call in router.calls:
        assert call.get("fallbacks") == [], (
            f"{call['model']} was called with the router's fallback chain live"
        )


def test_the_answer_is_attributed_to_the_deployment_that_produced_it():
    router = FakeRouter({"strong": [_chunk("from strong")], "cheap": [_chunk("from cheap")]})
    runs = _run(compare_question(
        router, [("strong", "groq/x"), ("cheap", "ollama/y")], MESSAGES, CONTEXT))
    answers = {r.deployment: r.answer for r in runs}
    assert answers == {"strong": "from strong", "cheap": "from cheap"}


# --- 2. shared context -----------------------------------------------------


def test_every_deployment_receives_identical_messages():
    router = FakeRouter({"strong": [_chunk("a")], "cheap": [_chunk("b")]})
    _run(compare_question(
        router, [("strong", "groq/x"), ("cheap", "ollama/y")], MESSAGES, CONTEXT))

    sent = [c["messages"] for c in router.calls]
    assert len(sent) == 2
    assert sent[0] == sent[1], "deployments saw different messages — not a controlled run"
    assert sent[0] is MESSAGES, "the harness rebuilt the prompt instead of reusing it"


def test_a_deployment_cannot_mutate_what_the_next_one_sees():
    """Shared state means a mutation by one row silently changes the rest."""
    router = FakeRouter({"strong": [_chunk("a")], "cheap": [_chunk("b")]})
    before = [dict(m) for m in MESSAGES]
    _run(compare_question(
        router, [("strong", "groq/x"), ("cheap", "ollama/y")], MESSAGES, CONTEXT))
    assert MESSAGES == before


# --- 3. errors are data ----------------------------------------------------


def test_a_failing_deployment_is_recorded_not_raised():
    router = FakeRouter(
        {"strong": [_chunk("ok")], "cheap": [_chunk("also ok")]},
        errors={"fallback": RuntimeError("provider down")},
    )
    runs = _run(compare_question(
        router,
        [("strong", "groq/x"), ("fallback", "openrouter/z"), ("cheap", "ollama/y")],
        MESSAGES, CONTEXT,
    ))

    assert len(runs) == 3, "a dead provider ended the run"
    failed = [r for r in runs if r.error]
    assert len(failed) == 1
    assert "provider down" in failed[0].error
    assert failed[0].deployment == "fallback"
    # and the others still answered
    assert [r.answer for r in runs if not r.error] == ["ok", "also ok"]


def test_a_timeout_is_reported_as_an_error_rather_than_an_empty_answer():
    """An empty answer and a timeout look identical in a table unless one of them
    says so."""
    router = FakeRouter({}, errors={"strong": asyncio.TimeoutError()})
    run = _run(run_deployment(router, "strong", "groq/x", MESSAGES, CONTEXT))
    assert run.error is not None
    assert run.answer == ""


def test_an_error_row_renders_without_crashing_the_report():
    router = FakeRouter({}, errors={"strong": RuntimeError("boom")})
    runs = _run(compare_question(router, [("strong", "groq/x")], MESSAGES, CONTEXT))
    report = ComparisonReport(
        generated_at="2026-08-14T00:00:00+00:00", offering_id="course-v1:X",
        deployments=[{"deployment": "strong", "model": "groq/x"}],
        questions=[QuestionComparison("q01", "What is a cohort?", 2, 0.9, runs)],
    )
    md = render_markdown(report)
    assert "ERROR" in md
    assert "0/1" in md, "the summary should show nothing answered"


# --- 4. missing usage ------------------------------------------------------


def test_usage_is_captured_when_the_provider_reports_it():
    router = FakeRouter({"strong": [
        _chunk("hello"),
        _chunk(usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}),
    ]})
    run = _run(run_deployment(router, "strong", "groq/x", MESSAGES, CONTEXT))
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (120, 30, 150)


def test_usage_on_a_chunk_with_empty_choices_is_not_skipped():
    """The exact shape that breaks a reader which stops at the first contentless
    chunk — and the reason the pipeline reads usage before its choices guard."""
    router = FakeRouter({"strong": [
        _chunk("a"), _chunk("b"),
        _chunk(usage={"total_tokens": 99}),
    ]})
    run = _run(run_deployment(router, "strong", "groq/x", MESSAGES, CONTEXT))
    assert run.answer == "ab"
    assert run.total_tokens == 99


def test_a_provider_that_reports_no_usage_leaves_the_counts_unknown():
    """`ollama_chat` sends no usage on stream chunks. None, never 0."""
    router = FakeRouter({"cheap": [_chunk("local answer")]})
    run = _run(run_deployment(router, "cheap", "ollama_chat/qwen2.5:7b", MESSAGES, CONTEXT))
    assert run.prompt_tokens is None
    assert run.completion_tokens is None
    assert run.total_tokens is None
    assert run.answer == "local answer"


def test_unknown_usage_renders_as_a_dash_not_a_zero():
    """A zero would claim the model spent no tokens, which is a measurement.
    This is the absence of one."""
    router = FakeRouter({"cheap": [_chunk("local answer")]})
    runs = _run(compare_question(router, [("cheap", "ollama_chat/qwen2.5:7b")],
                                 MESSAGES, CONTEXT))
    report = ComparisonReport(
        generated_at="2026-08-14T00:00:00+00:00", offering_id="course-v1:X",
        deployments=[{"deployment": "cheap", "model": "ollama_chat/qwen2.5:7b"}],
        questions=[QuestionComparison("q01", "What is a cohort?", 2, 0.9, runs)],
    )
    md = render_markdown(report)
    assert UNKNOWN in md
    assert "| 0 | 0 | 0 |" not in md, "unreported usage was rendered as zero"


# --- deployment selection --------------------------------------------------


MODEL_LIST = [
    {"model_name": "strong", "litellm_params": {"model": "groq/llama-3.3-70b"}},
    {"model_name": "cheap", "litellm_params": {"model": "ollama_chat/qwen2.5:7b"}},
]


def test_all_registered_deployments_are_compared_by_default():
    assert deployments_from(MODEL_LIST) == [
        ("strong", "groq/llama-3.3-70b"),
        ("cheap", "ollama_chat/qwen2.5:7b"),
    ]


def test_the_filter_selects_a_subset_in_the_order_given():
    assert deployments_from(MODEL_LIST, "cheap,strong") == [
        ("cheap", "ollama_chat/qwen2.5:7b"),
        ("strong", "groq/llama-3.3-70b"),
    ]


def test_an_unknown_deployment_is_refused_rather_than_skipped():
    """A typo that quietly compares fewer models produces a report whose title
    is a lie."""
    import pytest

    with pytest.raises(SystemExit) as exc:
        deployments_from(MODEL_LIST, "strong,fallbcak")
    assert "fallbcak" in str(exc.value)


def test_deployments_pointing_at_the_same_model_are_flagged():
    """Before a second provider is configured, `strong` and `cheap` are the same
    model here. A table that looks like a comparison while comparing a model
    with itself is worse than no table."""
    same = [("strong", "ollama_chat/qwen2.5:7b"), ("cheap", "ollama_chat/qwen2.5:7b")]
    assert find_aliases(same) == [["cheap", "strong"]]


def test_distinct_models_raise_no_alias_warning():
    assert find_aliases([("strong", "groq/x"), ("cheap", "ollama/y")]) == []


def test_the_alias_warning_reaches_the_report():
    report = ComparisonReport(
        generated_at="2026-08-14T00:00:00+00:00", offering_id="course-v1:X",
        deployments=[{"deployment": "strong", "model": "ollama_chat/qwen2.5:7b"},
                     {"deployment": "cheap", "model": "ollama_chat/qwen2.5:7b"}],
        aliases=[["cheap", "strong"]],
    )
    assert "SAME model" in render_markdown(report)


# --- serialisation ---------------------------------------------------------


def test_the_json_report_round_trips():
    import json

    router = FakeRouter({"cheap": [_chunk("x")]})
    runs = _run(compare_question(router, [("cheap", "ollama/y")], MESSAGES, CONTEXT))
    report = ComparisonReport(
        generated_at="2026-08-14T00:00:00+00:00", offering_id="course-v1:X",
        deployments=[{"deployment": "cheap", "model": "ollama/y"}],
        questions=[QuestionComparison("q01", "q?", 2, 0.9, runs)],
    )
    parsed = json.loads(to_json(report))
    assert parsed["questions"][0]["runs"][0]["deployment"] == "cheap"
    assert parsed["questions"][0]["runs"][0]["total_tokens"] is None


def test_the_verifier_result_is_recorded_when_supplied():
    router = FakeRouter({"strong": [_chunk("a sentence with no support")]})
    run = _run(run_deployment(
        router, "strong", "groq/x", MESSAGES, CONTEXT,
        verify=lambda answer, chunks: 3,
    ))
    assert run.unsupported == 3


def test_no_verifier_leaves_groundedness_unknown_rather_than_zero():
    router = FakeRouter({"strong": [_chunk("text")]})
    run = _run(run_deployment(router, "strong", "groq/x", MESSAGES, CONTEXT))
    assert run.unsupported is None
