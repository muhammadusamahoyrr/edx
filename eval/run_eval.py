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

from harness import metrics as M
from harness.runner import (
    GENERATION_ARMS,
    generation_candidates,
    run_authorization,
    run_generation,
    run_retrieval,
)


def settings_tau() -> float:
    from coursemate_service.config import settings

    return settings.confidence_threshold


def load_dataset(path: Path) -> dict:
    """Minimal YAML subset parser so the harness needs no extra dependency in
    the service image. The dataset shape is fixed and simple; a full YAML
    library would be a container rebuild for no gain."""
    import re

    text = path.read_text(encoding="utf-8")
    offering = re.search(r'^offering_id:\s*"([^"]+)"', text, re.MULTILINE).group(1)

    def one_line_list(body: str) -> list[str]:
        """`["a", "b"]` on a single line — the only list shape this file uses."""
        return [x.strip().strip('"') for x in body.strip().strip("[]").split('", "') if x.strip()]

    questions, current = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- id:"):
            if current:
                questions.append(current)
            current = {
                "id": line.split(":", 1)[1].strip(), "expect": [], "covered": True,
                # Defaults keep every pre-existing case valid without editing it.
                "arm": "original", "history": [], "usage_key": None,
            }
        elif current is not None and line.startswith("question:"):
            current["question"] = line.split(":", 1)[1].strip().strip('"')
        elif current is not None and line.startswith("expect:"):
            current["expect"] = one_line_list(line.split(":", 1)[1])
        elif current is not None and line.startswith("covered:"):
            current["covered"] = line.split(":", 1)[1].strip() == "true"
        elif current is not None and line.startswith("arm:"):
            current["arm"] = line.split(":", 1)[1].strip().strip('"')
        elif current is not None and line.startswith("history:"):
            current["history"] = one_line_list(line.split(":", 1)[1])
        elif current is not None and line.startswith("usage_key:"):
            # Strip a trailing `# comment`, which these entries carry.
            v = line.split(":", 1)[1].split("#")[0].strip().strip('"')
            current["usage_key"] = v or None
        elif current is not None and line.startswith("paraphrase:"):
            # Back-compat: the paraphrase arm predates `arm:` and marks itself
            # with a boolean. Its own comment says the harness "can report them
            # as their own arm" — it never did, and this is what makes that true.
            if line.split(":", 1)[1].split("#")[0].strip() == "true":
                current["arm"] = "paraphrase"
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

    arm_of = {q["id"]: q.get("arm", "original") for q in questions}
    for r in retrieval:
        r.arm = arm_of.get(r.qid, "original")

    all_covered = [r for r in retrieval if r.covered]
    uncovered = [r for r in retrieval if not r.covered]

    # **The headline stays over the SINGLE-TURN arms only**, which is what it
    # measured before the conversational arms existed. Phase A appended 18 cases
    # and this number silently began averaging them in — so a figure BENCHMARKS
    # quotes as retrieval quality became a blend of retrieval quality and an
    # unfixed conversational defect. Arms are reported separately below; a blend
    # of arms is not a measurement of anything.
    covered = [r for r in all_covered if r.arm in GENERATION_ARMS]

    r_at_3 = [M.recall_at_k(r.retrieved, r.expected, 3) for r in covered]
    r_at_5 = [M.recall_at_k(r.retrieved, r.expected, 5) for r in covered]
    p_at_3 = [M.precision_at_k(r.retrieved, r.expected, 3) for r in covered]
    mrr = [M.reciprocal_rank(r.retrieved, r.expected) for r in covered]
    lat = [r.latency_ms for r in retrieval]

    def arm_block(rows):
        if not rows:
            return None
        return {
            "n": len(rows),
            "recall_at_1": round(statistics.fmean(
                M.recall_at_k(r.retrieved, r.expected, 1) for r in rows), 3),
            "recall_at_3": round(statistics.fmean(
                M.recall_at_k(r.retrieved, r.expected, 3) for r in rows), 3),
            "mrr": round(statistics.fmean(
                M.reciprocal_rank(r.retrieved, r.expected) for r in rows), 3),
            # The failure that matters most: missed AND still above tau, so the
            # pipeline answers confidently from the wrong lesson.
            "answered_from_wrong_content": sum(
                1 for r in rows
                if M.recall_at_k(r.retrieved, r.expected, 5) == 0.0
                and r.top_score >= settings_tau()
            ),
        }

    # On uncovered questions the retriever SHOULD return nothing above tau.
    tau = settings_tau()
    leaked = [r for r in uncovered if r.top_score >= tau]

    by_arm = {
        arm: arm_block([r for r in all_covered if r.arm == arm])
        for arm in dict.fromkeys(r.arm for r in all_covered)
    }

    # --- generation (sample) ---------------------------------------------
    # Single-turn arms only. See `generation_candidates` for why sampling a bare
    # follow-up would not measure generation.
    eligible = generation_candidates(questions)
    print(f"\n[2/3] generation sample (n={args.gen}) from {len(eligible)} "
          f"single-turn cases — slow, real model ...", flush=True)
    generation = run_generation(eligible, offering, args.gen)

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
        "dataset": {
            "questions": len(questions),
            "covered": len(all_covered),
            "uncovered": len(uncovered),
            "headline_scope": "original + paraphrase (single-turn arms)",
            "headline_n": len(covered),
        },
        "retrieval_by_arm": by_arm,
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
    print(f"  retrieval headline is {report['dataset']['headline_scope']}, "
          f"n={report['dataset']['headline_n']}")
    print()
    print(f"  {'arm':22s}{'n':>4}{'r@1':>8}{'r@3':>8}{'MRR':>8}{'wrong+answered':>17}")
    print("  " + "-" * 64)
    for arm, m in by_arm.items():
        print(f"  {arm:22s}{m['n']:>4}{m['recall_at_1']:>8.3f}{m['recall_at_3']:>8.3f}"
              f"{m['mrr']:>8.3f}{m['answered_from_wrong_content']:>17}")
    print()
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
