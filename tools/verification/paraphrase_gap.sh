#!/usr/bin/env bash
# How much worse is lexical retrieval when the question avoids the lesson's words?
#
#   MSYS_NO_PATHCONV=1 tools/verification/paraphrase_gap.sh
#
# The original gold set scores recall@3 = 1.000, which reads as "retrieval is
# excellent" and is really "the questions were written while looking at the
# corpus". This runs the ORIGINAL arm and the PARAPHRASE arm separately against
# the live index and prints both, because the gap between them is the number
# that justifies embeddings -- or does not.
#
# It also reports the misses that scored ABOVE the confidence threshold, which
# is the finding that matters more than the recall drop: the gate abstains on a
# weak match, but a confident match on the WRONG lesson sails through it and is
# answered.
#
# Retrieval only. No model is called, so this runs in seconds rather than the
# ~40s per question a CPU-bound local model costs.
set -eu

CM=tutor_local-coursemate-1
REPO="$(cd "$(dirname "$0")/.." && pwd)"

docker cp "$REPO/../eval/datasets/retrieval_gold.yaml" "$CM":/tmp/gold.yaml >/dev/null 2>&1 \
  || docker cp "$(cd "$(dirname "$0")/../.." && pwd)/eval/datasets/retrieval_gold.yaml" \
       "$CM":/tmp/gold.yaml >/dev/null

cat > /tmp/pg.py <<'PY'
import re

# The gold file is simple enough to parse without adding pyyaml to the service
# image for a one-off probe.
QUESTIONS, cur = [], None
for line in open("/tmp/gold.yaml", encoding="utf-8"):
    s = line.strip()
    if s.startswith("- id:"):
        cur = {"id": s.split(":", 1)[1].strip(), "paraphrase": False, "covered": True}
        QUESTIONS.append(cur)
    elif cur is None or not s or s.startswith("#"):
        continue
    elif s.startswith("question:"):
        cur["question"] = s.split(":", 1)[1].strip().strip('"')
    elif s.startswith("expect:"):
        cur["expect"] = re.findall(r'"([^"]+)"', s)
    elif s.startswith("covered:"):
        cur["covered"] = "true" in s.lower()
    elif s.startswith("paraphrase:"):
        cur["paraphrase"] = "true" in s.split("#")[0].lower()

from coursemate_service.config import settings
from coursemate_service.knowledge import get_store
from coursemate_service.knowledge.rerank import get_reranker

store, reranker = get_store(), get_reranker()
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
TAU = settings.confidence_threshold


def top3(question):
    cands = store.search(question, tenant=settings.tenant, offering_id=OFFERING,
                         limit=settings.retrieve_candidates)
    ranked = reranker.rerank(question, cands, top_k=3)
    return [(c.display_name or "", c.score) for c in ranked]


def arm(is_paraphrase):
    qs = [q for q in QUESTIONS
          if q.get("covered") and q.get("paraphrase", False) is is_paraphrase and q.get("expect")]
    hit1 = hit3 = 0
    misses = []
    for q in qs:
        got = top3(q["question"])
        names = [n for n, _ in got]
        want = q["expect"]
        if names and any(w.lower() in names[0].lower() for w in want):
            hit1 += 1
        if any(any(w.lower() in n.lower() for w in want) for n in names):
            hit3 += 1
        else:
            misses.append((q["id"], q["question"], want, names, got[0][1] if got else 0.0))
    return qs, hit1, hit3, misses


print("=" * 74)
for is_para, label in ((False, "ORIGINAL   question shares the lesson's own words"),
                       (True,  "PARAPHRASE question deliberately avoids them")):
    qs, hit1, hit3, misses = arm(is_para)
    if not qs:
        continue
    n = len(qs)
    print()
    print(f"{label}     n={n}")
    print(f"  recall@1 = {hit1/n:.3f}     recall@3 = {hit3/n:.3f}")
    for qid, qtext, want, names, score in misses:
        flag = "   <-- ABOVE TAU" if score >= TAU else ""
        print(f"    MISS {qid}: {qtext[:56]}")
        print(f"         wanted {want}")
        print(f"         got    {names}  (top {score:.3f}){flag}")
    confident = [m for m in misses if m[4] >= TAU]
    if misses:
        print()
        print(f"  confidence_threshold = {TAU}")
        print(f"  wrong AND above threshold: {len(confident)} of {n}")
        if confident:
            print("  -> these are ANSWERED, not abstained. The gate catches a weak")
            print("     match; it does not catch a confident match on the wrong lesson.")
print()
print("=" * 74)
PY
docker cp /tmp/pg.py "$CM":/tmp/pg.py >/dev/null
docker exec "$CM" python /tmp/pg.py
