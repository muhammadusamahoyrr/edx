"""How selective is `supporting_chunks` on a generated QUESTION, not a prose answer?

`quiz_generator.py` emits a citation for every retrieved chunk. `pipeline.py`
narrows to the chunks the text actually drew on, and says why: emitting all of
them makes a citation mean "we searched this" rather than "this contributed".

Before moving the generator onto the same rule, one thing has to be measured
rather than assumed. `supporting_chunks` was calibrated on prose ANSWERS. A
generated question is shorter, is phrased as an instruction rather than a
statement, and may legitimately share fewer content words with the lesson it was
built from. If the rule turns out to admit every chunk anyway, the change buys
nothing; if it rejects nearly all of them, it would strip citations off a feature
whose whole permission to exist (§9.0) is that it is labelled and cited.

**This script changes no behaviour.** It calls the real generator against the
real index and the real model, records what came back, and prints a
distribution. `--dry-run` re-reads a saved capture without calling a provider.

Run it where the index and the provider config live:

    docker exec tutor_local-coursemate-1 python /eval/measure_question_grounding.py

`enforce_enrollment` is switched off IN THIS PROCESS ONLY. The eval is not a
student and has no LMS session; the running service is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from coursemate_contracts.auth import StudentClaims
from coursemate_service.ai.verify import content_terms, supporting_chunks
from coursemate_service.config import settings

OFFERING = "course-v1:OpenedX+OEX101+2023"


def _claims() -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="eval-grounding", username="eval", offering_id=OFFERING,
        course_id=OFFERING, block_id="eval", roles=["student"], group_tokens=[],
        iat=now, exp=now + 3600,
    )


def _per_chunk_overlap(text: str, chunk_texts: list[str]) -> list[dict]:
    """What `supporting_chunks` sees, chunk by chunk.

    It is a BOOLEAN rule — a chunk supports if it shares at least one content
    word — so there is no threshold to tune. The shared-term COUNT is reported
    anyway, because "passes on one incidental word" and "passes on twenty" are
    very different kinds of pass and the boolean cannot tell them apart.
    """
    used = content_terms(text)
    rows = []
    for i, chunk in enumerate(chunk_texts):
        terms = content_terms(chunk)
        shared = terms & used
        rows.append({
            "index": i,
            "shared_terms": len(shared),
            "chunk_terms": len(terms),
            "passes": bool(shared),
            "sample": sorted(shared)[:6],
        })
    return rows


async def _generate(clo_id: str, band: str | None) -> dict | None:
    """One real generation, with the chunks it actually retrieved."""
    from coursemate_service.ai.quiz_generator import generator

    claims = _claims()
    source, _ = await asyncio.to_thread(
        generator._find_source, claims, clo_id, band  # noqa: SLF001 - eval reads internals
    )
    if source is None:
        return None
    context = await asyncio.to_thread(generator._fetch_context, source, claims)  # noqa: SLF001

    text, citations = "", 0
    async for frame in generator.stream(claims, clo_id=clo_id, difficulty_band=band):
        if frame.type.value == "token":
            text += frame.text or ""
        elif frame.type.value == "citation":
            citations += 1
        elif frame.type.value == "error":
            return {"clo_id": clo_id, "error": frame.error_code.value}

    return {
        "clo_id": clo_id,
        "band": band,
        "question": text,
        "source_question_id": source.question_id,
        "citations_emitted": citations,
        "chunk_texts": [c.text for c in context.chunks],
        "chunk_keys": [c.citation.usage_key for c in context.chunks],
    }


async def _answer(question: str) -> dict | None:
    """One real CHAT answer, with the chunks it retrieved — the comparison arm.

    Same index, same retriever, same gate. Only the text differs, which is the
    whole point: prose is what `supporting_chunks` was calibrated on.
    """
    from coursemate_service.ai.pipeline import pipeline
    from coursemate_service.ai.retrieval import CourseContextProvider
    from coursemate_contracts.chat import ChatRequest

    claims = _claims()
    context = await asyncio.to_thread(
        CourseContextProvider().fetch_sync, question, claims
    )
    if not context.chunks:
        return None

    text = ""
    async for frame in pipeline.stream(
        ChatRequest(question=question, history=[], mode="direct"), claims
    ):
        if frame.type.value == "token":
            text += frame.text or ""
        elif frame.type.value == "error":
            return {"clo_id": "chat", "error": frame.error_code.value}

    return {
        "clo_id": "chat",
        "question": text,
        "chunk_texts": [c.text for c in context.chunks],
    }


def _report(rows: list[dict], label: str) -> None:
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
    if not rows:
        print("  no samples")
        return

    kept_fractions, all_shared = [], []
    print(f"  {'CLO':<8}{'chunks':>7}{'supporting':>12}{'kept':>8}   shared-term counts")
    print(f"  {'-' * 68}")
    for r in rows:
        chunks = r["chunk_texts"]
        keep = supporting_chunks(r["question"], chunks)
        per = _per_chunk_overlap(r["question"], chunks)
        counts = [p["shared_terms"] for p in per]
        all_shared += counts
        frac = len(keep) / len(chunks) if chunks else 0.0
        kept_fractions.append(frac)
        print(f"  {r['clo_id']:<8}{len(chunks):>7}{len(keep):>12}{frac:>7.0%}   {counts}")

    print(f"\n  chunks kept: mean {statistics.fmean(kept_fractions):.0%}"
          f"  min {min(kept_fractions):.0%}  max {max(kept_fractions):.0%}")
    if all_shared:
        zero = sum(1 for c in all_shared if c == 0)
        print(f"  per-chunk shared content words: median {statistics.median(all_shared):.0f}"
              f"  min {min(all_shared)}  max {max(all_shared)}")
        print(f"  chunks sharing NOTHING: {zero}/{len(all_shared)} "
              f"({zero / len(all_shared):.0%}) — these are what narrowing would drop")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/data/question_grounding.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="re-read a saved capture; calls no provider")
    ap.add_argument("--repeats", type=int, default=2,
                    help="generations per CLO; the model is not deterministic")
    args = ap.parse_args()

    out = Path(args.out)
    if args.dry_run:
        if not out.exists():
            print(f"no capture at {out}; run without --dry-run first")
            return 1
        captured = json.loads(out.read_text(encoding="utf-8"))
    else:
        settings.enforce_enrollment = False  # this process only
        from coursemate_service.knowledge import get_examprep_store
        clos = [c.clo_id for c in get_examprep_store().clos(
            tenant=settings.tenant, offering_id=OFFERING)]
        print(f"offering {OFFERING}: {len(clos)} outcomes -> {clos}")

        captured = []
        for clo in clos:
            for n in range(args.repeats):
                print(f"  generating {clo} ({n + 1}/{args.repeats}) ...", flush=True)
                r = asyncio.run(_generate(clo, None))
                if r is None:
                    print(f"    no source question for {clo}")
                elif "error" in r:
                    print(f"    abstained/error: {r['error']}")
                else:
                    print(f"    {len(r['question'])} chars, "
                          f"{len(r['chunk_texts'])} chunks retrieved")
                    captured.append(r)
        # --- the comparison arm: prose answers on the same index ----------
        prose: list[dict] = []
        for q in ("What is the Open edX named release process?",
                  "Who maintains Open edX and what roles exist?",
                  "How do I configure a Tutor deployment?"):
            print(f"  answering {q!r} ...", flush=True)
            r = asyncio.run(_answer(q))
            if r is None:
                print("    nothing retrieved")
            elif "error" in r:
                print(f"    {r['error']}")
            else:
                print(f"    {len(r['question'])} chars, {len(r['chunk_texts'])} chunks")
                prose.append(r)

        payload = {"generated": captured, "prose": prose}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nsaved {len(captured)} generated + {len(prose)} prose samples to {out}")
        captured = payload

    if isinstance(captured, list):     # a capture from before the prose arm
        captured = {"generated": captured, "prose": []}

    _report(captured["generated"],
            "GENERATED PRACTICE QUESTIONS  (supporting_chunks on question text)")
    _report(captured["prose"],
            "PROSE CHAT ANSWERS  (the text supporting_chunks was calibrated on)")

    print("\nNOTE: supporting_chunks is a BOOLEAN any-overlap rule, not a threshold.")
    print("      A chunk is kept if it shares >= 1 content word with the text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
