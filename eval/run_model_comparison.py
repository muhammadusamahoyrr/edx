"""Route the same question to every configured deployment and compare.

This is the measurement the "multi-model routing" claim rests on. Two design
decisions decide whether the numbers mean anything at all:

**1. The deployment is PINNED, never routed.** Every call passes
`model="<deployment name>"` directly, so the Router serves that exact
deployment. Letting the chain resolve would measure *routing* — which model the
fallback logic happened to pick — and the whole point here is to hold the model
constant and vary nothing else. A comparison built on `model="strong"` with
fallbacks enabled reports whatever answered, and reports it under the wrong name.

**2. Retrieval runs ONCE per question and the same context feeds every
deployment.** FTS5 retrieval is deterministic, so re-running it per deployment
would *probably* return the same chunks — but "probably" is not a controlled
experiment, and a retrieval change mid-run would show up as a model difference.
One fetch, one message list, N deployments.

What this does NOT measure, stated because all three are easy to misread:

* **Latency compares hardware, not models.** A hosted GPU against local CPU
  inference is not a model property. `qwen2.5:7b` here is 25 s cold on CPU.
* **Free-tier throttling inflates hosted latency.** Pace with `--sleep`; a run
  that 429s partway is a partial set reported as a measurement.
* **Token counts are absent for some providers.** `ollama_chat` reports no usage
  on stream chunks, which is why the daily budget charges an estimate
  (LIMITATIONS §4.1). Missing usage renders as `—`, never as `0` — a zero would
  read as "this model used no tokens", which is a different and false claim.

Abstention is deliberately not a per-deployment column. The confidence gate runs
*before* the model is called, so it is a property of retrieval and would be
identical down every column. What varies with the model, and is reported, is
whether the answer stays inside the material it was given — measured with the
same `unsupported_sentences` verifier the tutor uses in production.

Run:
    make model-compare
    python eval/run_model_comparison.py --limit 5 --sleep 2
    python eval/run_model_comparison.py --deployments strong,cheap
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

HERE = Path(__file__).resolve().parent
GOLD = HERE / "datasets" / "retrieval_gold.yaml"
REPORTS = HERE / "reports"

#: Rendered wherever a provider reported no usage. Not "0": a zero is a
#: measurement, and this is the absence of one.
UNKNOWN = "—"


@dataclass
class DeploymentRun:
    """One deployment's answer to one question."""

    deployment: str
    model: str
    answer: str = ""
    chars: int = 0
    first_token_ms: float | None = None
    total_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: Sentences whose content words are largely absent from the retrieved
    #: material. None when no verifier was supplied.
    unsupported: int | None = None
    error: str | None = None


@dataclass
class QuestionComparison:
    qid: str
    question: str
    context_chunks: int
    top_score: float
    runs: list[DeploymentRun] = field(default_factory=list)


@dataclass
class ComparisonReport:
    generated_at: str
    offering_id: str
    deployments: list[dict[str, str]]
    questions: list[QuestionComparison] = field(default_factory=list)
    #: Deployment names that resolve to the same concrete model. Reported rather
    #: than hidden: before a second provider is configured, `strong` and `cheap`
    #: are the same model here, and a table that looks like a comparison while
    #: comparing a model with itself is worse than no table.
    aliases: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- deployment selection --------------------------------------------------


def deployments_from(
    model_list: list[dict[str, Any]], wanted: str | None = None
) -> list[tuple[str, str]]:
    """`(name, concrete model)` for each registered deployment, in order.

    `wanted` is a comma-separated filter. An unknown name is an error rather
    than a silent skip — a typo that quietly compares fewer models than asked
    for produces a report whose title is a lie.
    """
    available = [(m["model_name"], m["litellm_params"]["model"]) for m in model_list]
    if not wanted:
        return available

    by_name = dict(available)
    chosen: list[tuple[str, str]] = []
    for name in [n.strip() for n in wanted.split(",") if n.strip()]:
        if name not in by_name:
            raise SystemExit(
                f"unknown deployment {name!r}; registered: {', '.join(by_name) or '(none)'}"
            )
        chosen.append((name, by_name[name]))
    return chosen


def find_aliases(deployments: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Groups of deployment names pointing at the same concrete model."""
    by_model: dict[str, list[str]] = {}
    for name, model in deployments:
        by_model.setdefault(model, []).append(name)
    return [sorted(names) for names in by_model.values() if len(names) > 1]


# --- one call --------------------------------------------------------------


def _usage_of(chunk: Any) -> tuple[int | None, int | None, int | None]:
    """Token counts from a chunk, or `(None, None, None)`.

    Read from EVERY chunk, not just the last: a provider that reports usage
    sends it on a final chunk with an empty `choices` list, so a reader that
    stops at the first chunk without content skips the only one that carries it.
    That exact shape is why the pipeline reads usage before its `choices` guard.
    """
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None, None, None
    return (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        getattr(usage, "total_tokens", None),
    )


def _text_of(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    return (getattr(delta, "content", None) or "") if delta is not None else ""


async def run_deployment(
    router: Any,
    deployment: str,
    model: str,
    messages: list[dict[str, str]],
    context_texts: list[str],
    *,
    max_tokens: int = 512,
    timeout: float = 300.0,
    verify: Callable[[str, list[str]], int] | None = None,
) -> DeploymentRun:
    """Ask ONE deployment, by name, and measure it.

    A failure is recorded and returned, never raised: one dead provider must not
    end a run that is measuring resilience across providers. That would leave the
    report silent about exactly the deployment worth reporting on.
    """
    run = DeploymentRun(deployment=deployment, model=model)
    started = time.perf_counter()
    parts: list[str] = []

    try:
        response = await asyncio.wait_for(
            router.acompletion(
                model=deployment,  # PINNED. Never "strong" with fallbacks live.
                messages=messages,
                stream=True,
                max_tokens=max_tokens,
                # **Pinning the name is not enough on its own.** Observed live
                # 2026-08-14: with the local provider unreachable, a call pinned
                # to `strong` reported
                # `Received Model Group=strong / Available Model Group
                # Fallbacks=['cheap']` and the Router went on to try `cheap`.
                # Had `cheap` been healthy it would have answered, and its answer
                # would have been recorded in the `strong` row — the exact
                # mis-attribution this harness exists to avoid.
                fallbacks=[],
            ),
            timeout=timeout,
        )
        async for chunk in response:
            p, c, t = _usage_of(chunk)
            if p is not None:
                run.prompt_tokens = p
            if c is not None:
                run.completion_tokens = c
            if t is not None:
                run.total_tokens = t

            text = _text_of(chunk)
            if text:
                if run.first_token_ms is None:
                    run.first_token_ms = (time.perf_counter() - started) * 1000
                parts.append(text)
    except Exception as exc:  # noqa: BLE001 — every provider fault is data here
        run.error = f"{type(exc).__name__}: {exc}"

    run.total_ms = (time.perf_counter() - started) * 1000
    run.answer = "".join(parts)
    run.chars = len(run.answer)
    if verify is not None and run.answer:
        run.unsupported = verify(run.answer, context_texts)
    return run


async def compare_question(
    router: Any,
    deployments: list[tuple[str, str]],
    messages: list[dict[str, str]],
    context_texts: list[str],
    *,
    sleep: float = 0.0,
    **kwargs: Any,
) -> list[DeploymentRun]:
    """Every deployment, same messages, one at a time.

    Sequential on purpose. Concurrency would overlap free-tier rate limits and
    contend for one local CPU, so the latency column would measure the harness
    rather than the providers.
    """
    runs: list[DeploymentRun] = []
    for i, (name, model) in enumerate(deployments):
        if sleep and i:
            await asyncio.sleep(sleep)
        runs.append(
            await run_deployment(router, name, model, messages, context_texts, **kwargs)
        )
    return runs


# --- reporting -------------------------------------------------------------


def _cell(value: int | float | None, suffix: str = "") -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, float):
        return f"{value:.0f}{suffix}"
    return f"{value}{suffix}"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def render_markdown(report: ComparisonReport) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Model comparison")
    add("")
    add(f"Generated {report.generated_at} · offering `{report.offering_id}` · "
        f"{len(report.questions)} question(s)")
    add("")
    add("**The same retrieved context was used for every deployment**, fetched once "
        "per question. Each call pinned its deployment by name; nothing was routed.")
    add("")
    add("Read with three caveats:")
    add("")
    add("* **Latency compares hardware, not models** — hosted GPU against local CPU.")
    add("* **Free-tier throttling inflates hosted latency.**")
    add(f"* **`{UNKNOWN}` means the provider reported no usage**, not zero tokens.")
    add("")

    for warning in report.warnings:
        add(f"> ⚠️ {warning}")
        add("")

    for group in report.aliases:
        add(f"> ⚠️ `{'`, `'.join(group)}` are the SAME model — these columns are not "
            "a comparison.")
        add("")

    add("## Deployments")
    add("")
    add("| deployment | model |")
    add("|---|---|")
    for d in report.deployments:
        add(f"| `{d['deployment']}` | `{d['model']}` |")
    add("")

    add("## Summary")
    add("")
    add("| deployment | answered | median total | median first token | median chars "
        "| unsupported | errors |")
    add("|---|---|---|---|---|---|---|")
    for d in report.deployments:
        name = d["deployment"]
        runs = [r for q in report.questions for r in q.runs if r.deployment == name]
        ok = [r for r in runs if not r.error]
        unsupported = [r.unsupported for r in ok if r.unsupported is not None]
        add(
            f"| `{name}` | {len(ok)}/{len(runs)} "
            f"| {_cell(_median([r.total_ms for r in ok]), ' ms')} "
            f"| {_cell(_median([r.first_token_ms for r in ok if r.first_token_ms is not None]), ' ms')} "
            f"| {_cell(_median([float(r.chars) for r in ok]))} "
            f"| {sum(unsupported) if unsupported else UNKNOWN} "
            f"| {len(runs) - len(ok)} |"
        )
    add("")

    add("## Per question")
    add("")
    for q in report.questions:
        add(f"### {q.qid} — {q.question}")
        add("")
        add(f"{q.context_chunks} chunk(s) retrieved, top score {q.top_score:.3f}")
        add("")
        add("| deployment | total | first token | prompt | completion | total tok "
            "| chars | unsupported |")
        add("|---|---|---|---|---|---|---|---|")
        for r in q.runs:
            if r.error:
                add(f"| `{r.deployment}` | **ERROR** | {r.error} | | | | | |")
                continue
            add(
                f"| `{r.deployment}` | {_cell(r.total_ms, ' ms')} "
                f"| {_cell(r.first_token_ms, ' ms')} "
                f"| {_cell(r.prompt_tokens)} | {_cell(r.completion_tokens)} "
                f"| {_cell(r.total_tokens)} | {r.chars} | {_cell(r.unsupported)} |"
            )
        add("")
    return "\n".join(lines) + "\n"


def to_json(report: ComparisonReport) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False)


# --- orchestration ---------------------------------------------------------


def _load_gold(path: Path) -> tuple[str, list[dict]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["offering_id"], data.get("questions", [])


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare deployments on identical context.")
    parser.add_argument("--deployments", help="comma-separated subset, e.g. strong,cheap")
    parser.add_argument("--limit", type=int, default=5,
                        help="questions to run (default 5; generation is the slow part)")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds between calls. Free tiers 429 without pacing, "
                             "and a partial run reported as a measurement is worse "
                             "than no run.")
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--out", type=Path, default=REPORTS)
    parser.add_argument("--index", help="chunk index to read. Defaults to the "
                                        "configured path, which is right inside "
                                        "the service container; pass a COPY when "
                                        "running anywhere else.")
    args = parser.parse_args(argv)

    # Before the service imports below: `settings` is a module-level singleton
    # built at first import, so setting this afterwards would be ignored while
    # appearing to work.
    if args.index:
        import os

        os.environ["COURSEMATE_INDEX_PATH"] = args.index

    # Imported here, not at module import: the tests exercise the comparison
    # logic without a configured index or provider, and a top-level service
    # import would make that impossible.
    from coursemate_contracts.chat import Mode
    from coursemate_service.ai.client import build_model_list, get_router
    from coursemate_service.ai.prompts import build_messages
    from coursemate_service.ai.retrieval import CourseContextProvider
    from coursemate_service.ai.verify import unsupported_sentences
    from coursemate_service.config import settings

    from harness.runner import make_claims  # noqa: F401 — same claims the eval uses

    model_list = build_model_list()
    if not model_list:
        raise SystemExit("no provider configured — set COURSEMATE_STRONG_MODEL")

    deployments = deployments_from(model_list, args.deployments)
    aliases = find_aliases(deployments)

    warnings: list[str] = []
    if settings.mock_response:
        warnings.append(
            "COURSEMATE_MOCK_RESPONSE is set — every deployment returns the same "
            "canned string and this report measures nothing."
        )

    offering_id, questions = _load_gold(args.gold)
    questions = questions[: args.limit]

    claims = make_claims(offering_id)
    provider = CourseContextProvider()
    router = get_router()

    def verify(answer: str, chunk_texts: list[str]) -> int:
        return len(unsupported_sentences(answer, chunk_texts, settings.claim_support_threshold))

    report = ComparisonReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        offering_id=offering_id,
        deployments=[{"deployment": n, "model": m} for n, m in deployments],
        aliases=aliases,
        warnings=warnings,
    )

    for q in questions:
        # ONE fetch. Every deployment below sees exactly these chunks.
        context = await provider.fetch(q["question"], claims)
        context_texts = [c.text for c in context.chunks]
        messages = build_messages(
            question=q["question"],
            history=[],
            context=context,
            mode=Mode.DIRECT,
            require_grounding=settings.require_grounding,
        )

        print(f"  {q['id']}: {len(context.chunks)} chunk(s) …", flush=True)
        runs = await compare_question(
            router, deployments, messages, context_texts,
            sleep=args.sleep,
            max_tokens=settings.max_output_tokens,
            timeout=settings.model_timeout_seconds,
            verify=verify,
        )
        for r in runs:
            print(f"    {r.deployment:10s} {r.error or f'{r.total_ms:.0f} ms, {r.chars} chars'}",
                  flush=True)

        report.questions.append(
            QuestionComparison(
                qid=q["id"], question=q["question"],
                context_chunks=len(context.chunks), top_score=context.top_score,
                runs=runs,
            )
        )

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.replace(":", "").replace("-", "")
    (args.out / f"model_comparison_{stamp}.json").write_text(to_json(report), encoding="utf-8")
    md_path = args.out / f"model_comparison_{stamp}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"\nwrote {md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
