"""Retrieval baseline, per arm — including the conversational arms.

    python eval/run_conversation_eval.py --index <copy of the live index.db>

**What this measures.** The SHIPPING retrieval path — the same
`CourseIntelligence` boundary call the chat pipeline makes, on a query built by
the same `ai.query.retrieval_query()` the pipeline uses. The harness does not
reproduce that rule, it calls it, so the two cannot drift apart.

Arms are reported separately and must not be averaged. `original` and
`paraphrase` are single-turn; `multiturn`, `topic_change` and
`usage_key_conflict` carry a conversation. The conversational arms exist because
retrieval used to search on the bare question: before 2026-08-12 a follow-up like
"why?" was searched with no idea what "why" referred to, scoring r@3 = 0.333 with
7 of 12 cases retrieving unrelated content above the threshold and answering
anyway.

**Why this is separate from `run_eval.py`.** That harness measures retrieval,
generation and authorization together against a live container and a real model.
This one needs no model and no container — retrieval is milliseconds — so the
conversational baseline can be re-run on every change instead of at milestones.
Same reason `run_generation_eval.py` is its own script.

Retrieval-only is also what keeps the baseline honest: generation on a bare
"why?" would measure the model's ability to cope with bad context, which is a
different question from whether the context was right.
"""

from __future__ import annotations

import argparse
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

GOLD = Path(__file__).parent / "datasets" / "retrieval_gold.yaml"

for _k, _v in (
    ("COURSEMATE_JWT_SIGNING_KEY", "conversation-eval-not-a-real-secret-32b+"),
    ("COURSEMATE_SERVICE_CREDENTIAL", "conversation-eval-not-a-real-cred-32b+"),
    ("COURSEMATE_ENFORCE_ENROLLMENT", "false"),
    ("COURSEMATE_EXAMPREP_PATH", ":memory:"),
):
    os.environ.setdefault(_k, _v)

#: Arms whose questions stand alone. The conversational arms are everything else.
STANDALONE = ("original", "paraphrase")


def claims_for(offering: str):
    from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims

    now = int(time.time())
    return StudentClaims(
        sub="1", username="admin", course_id=offering, offering_id=offering,
        roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 900, iat=now,
    )


def retrieve(case: dict, offering: str, claims, limit: int):
    """One retrieval through the real boundary. Returns (names, scores, ms, query).

    The query comes from `harness.runner.build_query`, shared with `run_eval.py`
    — one implementation, so the two harnesses cannot report different numbers
    for the same case.
    """
    from coursemate_service.boundary.impl import boundary
    from harness.runner import build_query

    query = build_query(case)
    t0 = time.perf_counter()
    chunks = boundary.retrieve_course_context(query, offering, claims, limit)
    ms = (time.perf_counter() - t0) * 1000
    return ([c.display_name or c.block_id for c in chunks],
            [round(c.score, 3) for c in chunks], ms, query)


def hit_rank(retrieved: list[str], expected: list[str]) -> int | None:
    """1-indexed rank of the first expected block, or None."""
    for i, name in enumerate(retrieved, start=1):
        if name in expected:
            return i
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", required=True, help="a COPY of the live chunk index")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--arms", default="", help="comma-separated arms to run; default all")
    args = ap.parse_args(argv)

    os.environ["COURSEMATE_INDEX_PATH"] = args.index

    from coursemate_service.config import settings
    from run_eval import load_dataset

    data = load_dataset(GOLD)
    offering = data["offering_id"]
    claims = claims_for(offering)
    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}

    cases = [c for c in data["questions"] if c["covered"]]
    if wanted:
        cases = [c for c in cases if c["arm"] in wanted]

    tau = settings.confidence_threshold
    limit = settings.rerank_top_k

    results = []
    for case in cases:
        names, scores, ms, query = retrieve(case, offering, claims, limit)
        rank = hit_rank(names, case["expect"])
        top = scores[0] if scores else 0.0
        results.append({
            "id": case["id"], "arm": case["arm"], "question": case["question"],
            "history": case["history"], "usage_key": case["usage_key"],
            "query": query,
            "expect": case["expect"], "retrieved": names, "scores": scores,
            "rank": rank, "top_score": top,
            # The failure that matters most: not "we missed", but "we missed AND
            # answered anyway". Below tau the pipeline abstains, which is wrong
            # but honest; above tau it answers confidently from wrong content.
            "answered_wrong": rank is None and top >= tau,
            "latency_ms": round(ms, 2),
        })

    def arm_metrics(rows):
        n = len(rows)
        if not n:
            return None
        hit1 = sum(1 for r in rows if r["rank"] == 1) / n
        hit3 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 3) / n
        mrr = statistics.fmean(1 / r["rank"] if r["rank"] else 0.0 for r in rows)
        return {
            "n": n,
            "recall_at_1": round(hit1, 3),
            "recall_at_3": round(hit3, 3),
            "mrr": round(mrr, 3),
            "answered_from_wrong_content": sum(1 for r in rows if r["answered_wrong"]),
            "median_latency_ms": round(statistics.median(r["latency_ms"] for r in rows), 2),
        }

    arms = {}
    for arm in dict.fromkeys(r["arm"] for r in results):
        arms[arm] = arm_metrics([r for r in results if r["arm"] == arm])

    out = {
        "environment": {
            "index": args.index,
            "offering": offering,
            "tau": tau,
            "rerank_top_k": limit,
            "retrieval_query": "ai.query.retrieval_query() — the shipping seam",
        },
        "arms": arms,
        "failures": [r for r in results if r["rank"] is None],
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    W = 100
    print("=" * W)
    print("RETRIEVAL BASELINE BY ARM")
    print("=" * W)
    for k, v in out["environment"].items():
        print(f"  {k:18s} {v}")
    print()
    print(f"  {'arm':20s}{'n':>4}{'r@1':>8}{'r@3':>8}{'MRR':>8}{'wrong+answered':>17}{'p50 ms':>9}")
    print("  " + "-" * (W - 4))
    for arm, m in arms.items():
        flag = "  <-- " + "!" * m["answered_from_wrong_content"] if m["answered_from_wrong_content"] else ""
        print(f"  {arm:20s}{m['n']:>4}{m['recall_at_1']:>8.3f}{m['recall_at_3']:>8.3f}"
              f"{m['mrr']:>8.3f}{m['answered_from_wrong_content']:>17}{m['median_latency_ms']:>9.2f}{flag}")

    print()
    print("-" * W)
    print(f"FAILURES — {len(out['failures'])} of {len(results)} cases retrieved no expected block")
    print("-" * W)
    for r in out["failures"]:
        verdict = "ANSWERED ANYWAY" if r["answered_wrong"] else "abstains (below tau)"
        print(f"\n  [{r['id']}  {r['arm']}]  {verdict}")
        if r["history"]:
            print(f"      history : {r['history']}")
        print(f"      asked   : {r['question']!r}")
        if r["query"] != r["question"]:
            print(f"      searched: {r['query']!r}")
        print(f"      expected: {r['expect']}")
        got = ", ".join(f"{n} ({s})" for n, s in zip(r["retrieved"], r["scores"], strict=False))
        print(f"      got     : {got or '<nothing>'}")

    print()
    print("-" * W)
    print("The query is built by ai.query.retrieval_query(), the same function the")
    print("pipeline uses — the harness cannot drift from production.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
