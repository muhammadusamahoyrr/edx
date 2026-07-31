"""CourseMate evaluation entrypoint.

    docker exec tutor_local-coursemate-1 python /eval/run_eval.py [--gen N]

Reproducibility is a design goal, not a nicety: the report records the model,
the index version, the chunk count and the dataset, so two runs can be compared
and a difference attributed to something. A benchmark whose environment is not
captured measures the weather.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from harness import metrics as M  # noqa: E402
from harness.runner import (  # noqa: E402
    run_authorization,
    run_generation,
    run_retrieval,
)


def load_dataset(path: Path) -> dict:
    """Minimal YAML subset parser so the harness needs no extra dependency in
    the service image. The dataset shape is fixed and simple; a full YAML
    library would be a container rebuild for no gain."""
    import re

    text = path.read_text(encoding="utf-8")
    offering = re.search(r'^offering_id:\s*"([^"]+)"', text, re.M).group(1)

    questions, current = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- id:"):
            if current:
                questions.append(current)
            current = {"id": line.split(":", 1)[1].strip(), "expect": [], "covered": True}
        elif current is not None and line.startswith("question:"):
            current["question"] = line.split(":", 1)[1].strip().strip('"')
        elif current is not None and line.startswith("expect:"):
            body = line.split(":", 1)[1].strip()
            current["expect"] = [x.strip().strip('"') for x in body.strip("[]").split(",") if x.strip()]
        elif current is not None and line.startswith("covered:"):
            current["covered"] = line.split(":", 1)[1].strip() == "true"
    if current:
        questions.append(current)
    return {"offering_id": offering, "questions": questions}


def environment(offering_id: str) -> dict:
    from coursemate_service.config import settings
    from coursemate_service.knowledge import get_store

    stats = get_store().stats(offering_id)
    return {
        "captured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "strong_model": settings.strong_model,
        "mock_response": bool(settings.mock_response),
        "require_grounding": settings.require_grounding,
        "confidence_threshold": settings.confidence_threshold,
        "rerank_top_k": settings.rerank_top_k,
        "enforce_enrollment": settings.enforce_enrollment,
        "index_chunks": stats.get("chunk_count"),
        "index_version": stats.get("active_version"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", type=int, default=6, help="generation sample size")
    ap.add_argument("--out", default="/eval/reports/latest.json")
    args = ap.parse_args()

    data = load_dataset(Path(__file__).parent / "datasets" / "retrieval_gold.yaml")
    offering = data["offering_id"]
    questions = data["questions"]

    env = environment(offering)
    print("=" * 70)
    print("COURSEMATE EVALUATION")
    print("=" * 70)
    for k, v in env.items():
        print(f"  {k:22s} {v}")

    # --- retrieval (all questions) --------------------------------------
    print(f"\n[1/3] retrieval over {len(questions)} questions ...", flush=True)
    retrieval = run_retrieval(questions, offering)

    covered = [r for r in retrieval if r.covered]
    uncovered = [r for r in retrieval if not r.covered]

    r_at_3 = [M.recall_at_k(r.retrieved, r.expected, 3) for r in covered]
    r_at_5 = [M.recall_at_k(r.retrieved, r.expected, 5) for r in covered]
    p_at_3 = [M.precision_at_k(r.retrieved, r.expected, 3) for r in covered]
    mrr = [M.reciprocal_rank(r.retrieved, r.expected) for r in covered]
    lat = [r.latency_ms for r in retrieval]

    # On uncovered questions the retriever SHOULD return nothing above tau.
    from coursemate_service.config import settings
    tau = settings.confidence_threshold
    leaked = [r for r in uncovered if r.top_score >= tau]

    # --- generation (sample) ---------------------------------------------
    print(f"\n[2/3] generation sample (n={args.gen}) — slow, real model ...", flush=True)
    generation = run_generation(questions, offering, args.gen)

    abst = M.AbstentionOutcome()
    ground_scores, cite_scores, ttfts, totals = [], [], [], []
    unsupported_examples: list[str] = []

    for g in generation:
        if g.covered:
            if g.abstained:
                abst.false_abstentions += 1
            else:
                abst.correct_answers += 1
                gr = M.groundedness(g.answer, g.context_texts)
                if gr.score == gr.score:  # not NaN
                    ground_scores.append(gr.score)
                    unsupported_examples.extend(gr.unsupported_sentences[:1])
                cc = M.citation_correctness(g.citations, [c[:60] for c in g.context_texts] + g.citations)
                if cc == cc:
                    cite_scores.append(cc)
        else:
            if g.abstained:
                abst.correct_abstentions += 1
            else:
                abst.false_answers += 1
        if g.ttft_ms:
            ttfts.append(g.ttft_ms)
        if g.total_ms:
            totals.append(g.total_ms)

    # --- authorization ----------------------------------------------------
    print("\n[3/3] authorization matrix ...", flush=True)
    authz = run_authorization(offering)

    def avg(xs):
        xs = [x for x in xs if x == x]
        return round(statistics.fmean(xs), 3) if xs else None

    report = {
        "environment": env,
        "dataset": {"questions": len(questions), "covered": len(covered), "uncovered": len(uncovered)},
        "retrieval": {
            "recall_at_3": avg(r_at_3),
            "recall_at_5": avg(r_at_5),
            "precision_at_3": avg(p_at_3),
            "mrr": avg(mrr),
            "latency_p50_ms": round(M.percentile(lat, 50), 2),
            "latency_p95_ms": round(M.percentile(lat, 95), 2),
            "uncovered_above_tau": len(leaked),
            "uncovered_total": len(uncovered),
            "misses": [
                {"qid": r.qid, "q": r.question[:60], "expected": r.expected, "got": r.retrieved[:3]}
                for r in covered if M.recall_at_k(r.retrieved, r.expected, 5) == 0.0
            ],
        },
        "generation": {
            "sample_size": len(generation),
            "groundedness_mean": avg(ground_scores),
            "hallucination_rate": (round(1 - avg(ground_scores), 3) if avg(ground_scores) is not None else None),
            "citation_correctness": avg(cite_scores),
            "ttft_p50_ms": round(M.percentile(ttfts, 50), 1) if ttfts else None,
            "ttft_p95_ms": round(M.percentile(ttfts, 95), 1) if ttfts else None,
            "total_p50_ms": round(M.percentile(totals, 50), 1) if totals else None,
            "unsupported_examples": unsupported_examples[:3],
        },
        "abstention": {
            "false_answer_rate": (round(abst.false_answer_rate, 3) if abst.false_answer_rate == abst.false_answer_rate else None),
            "false_abstention_rate": (round(abst.false_abstention_rate, 3) if abst.false_abstention_rate == abst.false_abstention_rate else None),
            "correct_answers": abst.correct_answers,
            "correct_abstentions": abst.correct_abstentions,
            "false_answers": abst.false_answers,
            "false_abstentions": abst.false_abstentions,
        },
        "authorization": authz,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(json.dumps(report["retrieval"], indent=2)[:900])
    print(json.dumps(report["generation"], indent=2))
    print(json.dumps(report["abstention"], indent=2))
    for row in authz:
        print(f"  [{'PASS' if row['pass'] else 'FAIL'}] {row['case']:26s} "
              f"expected={row['expected']:5s} actual={row['actual']}")
    print(f"\nwritten to {out}")
    return 0 if all(r["pass"] for r in authz) else 1


if __name__ == "__main__":
    raise SystemExit(main())
