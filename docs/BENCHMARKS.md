# Benchmark Report

*All figures produced by `eval/run_eval.py` against a live Open edX instance with
the real model. Reproduction command and environment are recorded with each run.*

---

## 1. Methodology, and why these metrics

The governing decision is to **score retrieval and generation separately**.
Measuring only the answer hides retrieval failures — the documented case is a
legal RAG scoring 0.91 faithfulness while missing a key statute one time in six;
only context recall exposed the retriever.

That decision paid off immediately: the first run showed **groundedness 1.000 and
hallucination 0.000 while the system answered every off-topic question**. The
model faithfully grounded its answers in irrelevant chunks. Generation metrics
alone would have called that a success.

| Metric | Why this one |
|---|---|
| **recall@k** (leads) | A generator can ignore an irrelevant chunk but **cannot invent a missing one** |
| **MRR** | Top chunks dominate the prompt; rank 1 ≠ rank 5 |
| precision@k | Noise consumes context budget |
| **Groundedness** | Fraction of answer *claims* supported by retrieved text |
| Citation correctness | A wrong citation is worse than none — it manufactures the appearance of grounding |
| **Abstention, both directions** | Tuning only against false answers yields a tutor that refuses everything and scores perfectly |
| Latency **p95** | A mean hides the requests students complain about |
| Authorization **matrix** | The value is in the denials |

**No LLM-as-judge, deliberately.** A model grading a model answers *"does another
model find this plausible?"*, not *"is this correct?"* — and a judge that drifts
cannot tell you whether your retrieval change helped or the judge moved.
Determinism is what makes a benchmark a benchmark. The cost is precision:
token-overlap groundedness is a **floor**, not a verdict.

### Dataset

18 questions built by **sampling the actual index**, not invented — a gold set
written without looking at the corpus measures the author's imagination. 12
covered, 6 uncovered (4 clearly off-topic, 2 adversarial: plausible-sounding
platform questions absent from this course).

**n=18 is small.** Every figure below should be read with that attached.

---

## 2. Environment

| | |
|---|---|
| Model | `ollama_chat/qwen2.5:7b` (local, CPU) |
| Mock | **disabled** — real generation |
| Index | 231 chunks from 226 leaf blocks |
| Course | `course-v1:OpenedX+DemoX+DemoCourse` |
| τ | 0.35 |
| Enrollment enforcement | on |

---

## 3. Results

### 3.1 Retrieval — reranker A/B

| Metric | OFF (control) | ON (lexical) | Δ |
|---|---|---|---|
| **recall@3** | 0.833 | **1.000** | **+0.167** |
| recall@5 | 1.000 | 1.000 | — |
| **precision@3** | 0.306 | **0.389** | +0.083 |
| **MRR** | 0.644 | **0.833** | **+0.189** |
| latency p95 | 11.88 ms | 12.15 ms | +0.27 ms |
| uncovered above τ | 0/6 | 0/6 | — |

**MRR is the headline.** The correct chunk moved from typically rank 2 to rank 1.
Reranking found nothing new — the candidate pool is identical — it promoted
correct chunks from positions 4–5 into the top 3. That is exactly why the boundary
retrieves 20 candidates and reranks to 3.

**precision@3 is the least trustworthy number here.** The gold set marks 1–2
blocks correct per question, but the course genuinely discusses topics across
several blocks, so some counted "noise" is plausibly relevant content the labels
don't credit. It is partly measuring my labelling.

**No safety regression:** the reranker rewrites the score the confidence gate
reads, so the risk was promoting off-topic content above τ. 0/6 leaked in both arms.

### 3.2 Generation and abstention

| Metric | Before gate fix | After |
|---|---|---|
| **false-answer rate** | **1.000** | **0.000** |
| false-abstention rate | 0.000 | 0.000 |
| correct abstentions | 0 / 6 | **3 / 3** |
| groundedness | 1.000 | 0.914 |
| citation correctness | 1.000 | 1.000 |

**Abstention costs 3 ms.** The gate fires before generation, so refusing is free —
no model call, no spend.

**Groundedness *fell* and that is an improvement.** The earlier 1.000 was measured
on answers grounded in irrelevant chunks. The 0.914 is measured on real answers,
and the two flagged sentences were a lead-in and a pointer — connective prose, not
claims. `is_claim()` now excludes those, with the exclusion count reported.

### 3.3 Latency

| Stage | p50 | p95 |
|---|---|---|
| Retrieval (incl. rerank) | 1.1 ms | **12 ms** |
| JWT mint | 0.115 ms | — |
| Abstention (end to end) | **3 ms** | — |
| Time to first token (CPU, 7B) | 24 s | 110 s |

**Retrieval is not the bottleneck; inference is, by four orders of magnitude.**
Against the 2 s design budget, local CPU inference misses by 12×–55× — which
confirms the design's own prediction that a CPU-hosted model *"would not meet an
800 ms first token — several times that"*.

⚠️ **Latency figures are not comparable across runs.** Ollama's model cache state
differs, and runs with more abstentions have lower medians because 3 ms
abstentions sit in the distribution. Treat inference latency as order-of-magnitude
only.

### 3.4 Authorization — 4/4 pass

| Case | Expected | Actual |
|---|---|---|
| enrolled user | allow | **allow** (5 chunks) |
| unknown user | deny | **deny** |
| cross-offering request | deny | **deny** |
| expired token | deny | **deny** (at `api/deps.py`) |

Plus, in unit tests: platform unreachable → **deny** (fails closed); cache expires
so revocation takes effect; cache keyed per user *and* offering.

---

## 4. Bugs the benchmark found

The benchmark's value was not the numbers. It was these.

### 4.1 The confidence gate could never fire — CRITICAL

`store.search` normalised BM25 against the best row **of the same query**:

```python
best = min(r["raw"] for r in rows)
score = raw / best          # top hit is ALWAYS exactly 1.0
```

So `top_score < threshold` was unreachable while any row came back. Only the
zero-hit case ever abstained. The tutor answered *"explain quantum
chromodynamics"* from an unrelated lesson.

**Root cause: a relative quantity used as an absolute threshold.** BM25 magnitude
is corpus- and query-dependent — excellent for ordering, meaningless as
confidence.

**Fix:** BM25 **orders**, query-term coverage **gates** — bounded 0–1, comparable
across queries, interpretable. Measured after: covered questions 1.000, uncovered
0.200–0.250, τ=0.35 cleanly between.

### 4.2 Multi-batch indexing silently discarded 88% of the course

Each ingest batch performed its own write→verify→swap, and swap deactivates every
other version. A 226-block course ended up serving **26 blocks while every batch
reported success.** Nothing failed. Fixed with `run_id` + `is_final`.

### 4.3 FTS5 injection via a student's question

Stripping punctuation left FTS5 keywords intact, so `cats AND dogs` was parsed as
operators → `sqlite3.OperationalError`. A student could 500 the endpoint by typing
"OR". Fixed by quoting each term as a literal.

### 4.4 The groundedness metric penalised connective prose

Flagged `"...as highlighted in the course material:"` as hallucination. The metric
was measuring discourse style, not fidelity.

---

## 5. Reproduction

```bash
bash tools/verification/stage_eval.sh
docker exec tutor_local-coursemate-1 python /eval/run_eval.py --gen 6
# → /eval/reports/latest.json
```

Retrieval is measured on all 18 questions; generation is sampled at 6. Full
generation coverage costs ~12 minutes of CPU inference and, at this sample size,
would not change the conclusions.

---

## 6. What the numbers do not say

1. **recall@3 = 1.000 means the benchmark is saturated.** It can no longer
   distinguish improvement. Before adding embeddings, the gold set needs
   paraphrase questions — *"what helps deaf learners follow videos?"* → Transcripts
   — which is exactly where lexical retrieval should fail.
2. **Groundedness is a floor**, measured by token overlap, not entailment.
3. **n=18, single rater (the author).** Indicative, not settled.
4. **One course, one model.** DemoX is curated; real institutional courses are
   messier.
