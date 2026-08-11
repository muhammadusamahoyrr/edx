"""Feature B generation evaluation — quality, safety and latency.

    python eval/run_generation_eval.py --index <copy of the live index.db>

Runs the REAL `QuizGenerator` against a REAL chunk index and a loaded pack, then
scores what it produced with `feature_b_rubric.py`. Nothing is stubbed except the
clock: the boundary, the confidence gate, the model routing and the contract
validation are all the shipping ones.

**LIMITATION, stated up front.** The *lesson content* is real — the OEX101 course
as indexed on the live stack. The *source questions* are exam-style items written
against those lessons for this eval, not extracted from a real past paper, because
no real paper exists for a demo course. So this measures the generator, not the
extractor: "given a plausible source question and real course material, does the
pipeline produce something the rubric accepts?" Phase 2's PDF ingestion is what
would replace the authored half.

Two things are reported separately and must not be averaged together:

    QUALITY   the rubric's four metrics over questions that generated
    SAFETY    what happened on cases that must NOT generate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "coursemate-service"))
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))
sys.path.insert(0, str(Path(__file__).parent))

PACK = Path(__file__).parent / "datasets" / "generation_pack.json"

for _k, _v in (
    ("COURSEMATE_JWT_SIGNING_KEY", "generation-eval-not-a-real-secret-32b+"),
    ("COURSEMATE_SERVICE_CREDENTIAL", "generation-eval-not-a-real-cred-32b+"),
    ("COURSEMATE_ENFORCE_ENROLLMENT", "false"),
    ("COURSEMATE_EXAMPREP_PATH", str(Path(__file__).parent / "_eval_examprep.db")),
):
    os.environ.setdefault(_k, _v)


def _claims(offering: str):
    from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims

    now = int(time.time())
    return StudentClaims(
        sub="1", username="admin", course_id=offering, offering_id=offering,
        roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 3600, iat=now,
    )


# --- per-stage timing ------------------------------------------------------


class Timed:
    """Wraps the generator's own methods. Measures the shipping code paths, not
    a reimplementation of them — the agent profile learned that lesson."""

    def __init__(self, gen):
        self.gen = gen
        self.marks: dict[str, float] = {}
        self._patch()

    def _patch(self):
        real_find = self.gen._find_source
        real_ctx = self.gen._fetch_context
        real_parse = self.gen._parse
        real_dup = self.gen._is_reprint

        def timed(name, fn):
            def inner(*a, **k):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **k)
                finally:
                    self.marks[name] = self.marks.get(name, 0.0) + time.perf_counter() - t0
            return inner

        self.gen._find_source = timed("source_retrieval", real_find)
        self.gen._fetch_context = timed("context_retrieval", real_ctx)
        self.gen._parse = timed("validation", real_parse)
        self.gen._is_reprint = timed("duplicate_check", real_dup)

    def reset(self):
        self.marks = {}


async def generate_one(gen, timer, claims, clo_id, band):
    """One turn. Returns (PracticeQuestion-shaped dict | None, outcome, timings)."""
    from coursemate_contracts.chat import FrameType

    timer.reset()
    t0 = time.perf_counter()
    ttft = None
    text_parts, outcome = [], "answer"

    async for frame in gen.stream(claims, clo_id=clo_id, difficulty_band=band):
        if frame.type is FrameType.TOKEN:
            if ttft is None:
                ttft = time.perf_counter() - t0
            text_parts.append(frame.text or "")
        elif frame.type is FrameType.ERROR:
            outcome = frame.error_code.value
    total = time.perf_counter() - t0

    timings = {**timer.marks, "ttft": ttft, "total": total}
    return ("".join(text_parts) or None), outcome, timings


async def preflight(gen, claims, pack) -> str | None:
    """One cheap call to check the provider can do what the generator needs.

    **What the generator actually requires is narrow**, and worth stating because
    it is a much lower bar than the agent's: a non-streaming completion whose
    `message.content` is JSON containing a `question` key. No tool calling, no
    structured-output API, no parallel calls. Any OpenAI-compatible provider that
    can follow "reply with JSON" qualifies.

    Verified rather than assumed: `qwen/qwen3.6-27b` on Groq emits a `<think>`
    reasoning preamble before its JSON and fails to parse, so "hosted and
    OpenAI-compatible" is not sufficient on its own.

    Returns None when usable, else a reason. Runs BEFORE the 20 questions so an
    incompatible or rate-limited provider costs one call instead of twenty.
    """
    q = pack.questions[0]
    try:
        _, outcome, _ = await generate_one(gen, Timed(gen), claims, q.clo_id, None)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    if outcome == "unavailable":
        return "provider returned UNAVAILABLE (rate limit, auth, or unreachable)"
    if outcome == "abstained":
        return ("the pipeline abstained on a known-good source question, so the "
                "provider is not producing parseable JSON with a `question` key")
    return None


def score_per_question(generated: list[tuple[str, str | None, dict]], bank: list[dict]):
    """Score each generated question against the CLO it was ASKED for.

    **Not one batch call.** `score()` takes a single `requested_clo`, and this set
    spans four outcomes, so a batch run has no CLO to check against and reports
    `clo_alignment` as "not run" — the metric silently absent rather than wrong.
    That is the same shape as the Phase 0.5 defect, and it was found by running
    the eval rather than by reading it.

    `generated` is `[(requested_clo, requested_band, question_dict), ...]`, so the
    pairing is carried in the data and cannot drift from the order of parallel
    lists. The band travels with it for the same reason the CLO does — both are
    per-question requests, and both quality checks need to know what was asked.
    """
    from feature_b_rubric import RubricReport, score

    report = RubricReport(n_questions=len(generated), n_bank=len(bank))
    for requested_clo, requested_band, item in generated:
        report.findings.extend(score(
            [item], bank, requested_clo=requested_clo, requested_band=requested_band
        ).findings)
    return report


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


async def main_async(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, help="a COPY of the chunk index")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--delay", type=float, default=13.0,
        help="seconds between generations. Groq's free tier allows 12k tokens per "
             "MINUTE and one turn costs ~2.5k, so ~4/min is the ceiling. Without "
             "pacing the run 429s partway and the result is a partial set "
             "reported as if it were a measurement.",
    )
    args = parser.parse_args(argv)

    os.environ["COURSEMATE_INDEX_PATH"] = args.index
    for stale in (Path(os.environ["COURSEMATE_EXAMPREP_PATH"]),):
        if stale.exists():
            stale.unlink()

    from coursemate_contracts.examprep import ExamPrepPack, PracticeQuestion, band_of
    from coursemate_service.ai.quiz_generator import QuizGenerator
    from coursemate_service.knowledge import get_examprep_store

    pack = ExamPrepPack(**json.loads(PACK.read_text(encoding="utf-8")))
    get_examprep_store().load_pack(pack)
    claims = _claims(pack.offering_id)

    gen = QuizGenerator()

    # --- preflight: never spend 20 calls proving the provider is unusable ----
    reason = await preflight(gen, claims, pack)
    if reason:
        print("=" * 92)
        print("PREFLIGHT FAILED — no eval was run, and no numbers are reported.")
        print(f"  {reason}")
        print("  A partial run is not a measurement. Fix the provider and re-run.")
        print("=" * 92)
        return 2

    timer = Timed(gen)

    # --- positive set: one generation per source question ------------------
    generated, outcomes, timings = [], [], []
    for n, q in enumerate(pack.questions):
        if n:
            await asyncio.sleep(args.delay)
        band = band_of(q.difficulty)
        text, outcome, t = await generate_one(gen, timer, claims, q.clo_id, band)
        if outcome == "unavailable":
            # Stop immediately. The previous run ground through 18 of these and
            # printed a 2/20 report that read like a result — exactly what must
            # not happen.
            print("=" * 92)
            print(f"ABORTED at {q.question_id} — the provider became unavailable "
                  "(rate limit or outage).")
            print(f"  {len(generated)} of {len(pack.questions)} generated. "
                  "That is not a measurement and no metrics are reported.")
            print("=" * 92)
            return 2
        outcomes.append((q.question_id, band, outcome))
        timings.append(t)
        if text:
            generated.append((q.clo_id, band, PracticeQuestion(
                text=text, clo_id=q.clo_id, ai_generated=True,
                derived_from=[q.question_id], marks=q.marks, difficulty=q.difficulty,
            ).model_dump()))

    # --- negative set: these must NOT generate -----------------------------
    negatives = [
        ("no matching past question", "CLO-NONEXISTENT", None),
        ("unsupported outcome", "CLO-99", "hard"),
    ]
    negative_results = []
    for label, clo, band in negatives:
        _, outcome, _ = await generate_one(gen, timer, claims, clo, band)
        negative_results.append((label, outcome, outcome != "answer"))

    # --- score --------------------------------------------------------------
    bank = [q.model_dump() for q in pack.questions]
    report = score_per_question(generated, bank)

    answered = [o for _, _, o in outcomes if o == "answer"]
    by_band: dict[str, list[str]] = {}
    for _, band, outcome in outcomes:
        by_band.setdefault(band or "unbanded", []).append(outcome)

    generated_items = [g for _, _, g in generated]
    out = {
        "n_source_questions": len(pack.questions),
        "n_generated": len(generated_items),
        "quality": report.as_dict(),
        "outcomes_by_band": {b: {o: v.count(o) for o in set(v)} for b, v in by_band.items()},
        "safety": [{"case": c, "outcome": o, "abstained": a} for c, o, a in negative_results],
        "latency_seconds": {
            stage: {
                "median": statistics.median(vals) if vals else None,
                "p95": pct(vals, 0.95),
            }
            for stage in ("source_retrieval", "context_retrieval", "duplicate_check",
                          "validation", "ttft", "total")
            for vals in [[t[stage] for t in timings if t.get(stage) is not None]]
        },
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    W = 92
    print("=" * W)
    print(f"FEATURE B GENERATION EVAL — {len(generated_items)}/{len(pack.questions)} generated")
    print("=" * W)
    print("  Source questions are exam-style items authored against the REAL indexed")
    print("  lessons, not extracted from a real paper. This measures the generator.")
    print()
    print("-" * W)
    print(f"QUALITY  (n = {len(generated_items)} generated, bank of {len(bank)})")
    print("-" * W)
    print("  QUALITY — reads the generated TEXT; these can fail")
    for metric, v in out["quality"]["quality"].items():
        shown = "not run" if v["rate"] is None else f"{v['rate']:.3f}"
        print(f"    {metric:24s} {shown:>8s}   n={v['n']}")
    print("  SAFETY  — reads injected metadata; invariants, expected 1.000")
    for metric, v in out["quality"]["safety"].items():
        shown = "not run" if v["rate"] is None else f"{v['rate']:.3f}"
        print(f"    {metric:24s} {shown:>8s}   n={v['n']}")
    if report.failures():
        print(f"  failures: {len(report.failures())}")
        for f in report.failures()[:6]:
            print(f"    [{f.question_index}] {f.check}: {f.detail[:70]}")

    print()
    print("-" * W)
    print("OUTCOMES BY BAND")
    print("-" * W)
    for band in ("easy", "medium", "hard", "unbanded"):
        if band in out["outcomes_by_band"]:
            print(f"  {band:10s} {out['outcomes_by_band'][band]}")

    print()
    print("-" * W)
    print("SAFETY  (these must abstain)")
    print("-" * W)
    for row in out["safety"]:
        print(f"  {'PASS' if row['abstained'] else 'FAIL'}  {row['case']:34s} -> {row['outcome']}")

    print()
    print("-" * W)
    print("LATENCY  (seconds; generator only, not the agent)")
    print("-" * W)
    print(f"  {'stage':22s}{'median':>10s}{'p95':>10s}")
    for stage, v in out["latency_seconds"].items():
        med = "—" if v["median"] is None else f"{v['median']:.3f}"
        p95 = "—" if v["p95"] is None else f"{v['p95']:.3f}"
        print(f"  {stage:22s}{med:>10s}{p95:>10s}")

    all_safe = all(r["abstained"] for r in out["safety"])
    return 0 if all_safe else 1


def main(argv=None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
