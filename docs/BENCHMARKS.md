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

## 3.5 The paraphrase arm — added 2026-08-05

The original 18 questions could no longer measure anything. recall@3 = 1.000, not
because retrieval is excellent but because the questions were written while
looking at the corpus and inherited its vocabulary. A gold set that asks *"what
are XBlocks?"* of a lesson titled **XBlocks** is testing string matching.

Ten paraphrase questions were added, each asking for the same content in words
the lesson does not use. Run against the live index
(`tools/verification/paraphrase_gap.sh`):

| Arm | n | recall@1 | recall@3 |
|---|---|---|---|
| Original — question shares the lesson's words | 12 | 0.750 | **1.000** |
| Paraphrase — question deliberately avoids them | 10 | 0.200 | **0.300** |

**recall@3 falls from 1.000 to 0.300.** That is the number LIMITATIONS §1 has
been asserting in prose since the beginning; it is now measured, and it is the
baseline any semantic retriever has to beat.

### The finding that matters more than the recall drop

Look at the scores on the misses, against `confidence_threshold = 0.35`:

```
MISS p05  wanted [Content Libraries]
          got    [How do discussions work?, ...]        top 0.386   <-- ABOVE TAU
MISS p10  wanted [ORA, Assessments Summary]
          got    [How do discussions work?, ...]        top 0.475   <-- ABOVE TAU
```

**2 of 10 are answered rather than abstained**, from the wrong lesson, with a
confident score. The confidence gate catches a *weak* match — it does not catch a
*confident match on the wrong content*. Those two questions produce a grounded,
cited, plausible answer drawn from a lesson that does not address the question.

This is a different failure from the one §8.5 was designed against, and it is
worse: abstention is visible to the student, a confidently wrong citation is not.
The claim verifier (§3.2 of LIMITATIONS) will mark sentences the retrieved text
does not support, which narrows the blast radius, but it cannot help when the
*retrieval itself* is confidently wrong — the answer will be faithfully grounded
in the wrong lesson.

**What this does not say.** Ten questions, one course, one author. It establishes
that the gap is real and roughly how large; it does not establish that embeddings
close it. That is the next measurement, and it now has something to be measured
against.

---

## 3.6 Feature B end to end, from a real PDF — added 2026-08-12

Everything above measures **retrieval and chat**. This section measures **Feature
B**, and it is the first run whose source questions were not written by hand.

**Why that distinction is the whole point of this section.** The earlier Feature B
numbers came from `eval/datasets/generation_pack.json` — exam-style items authored
against the real indexed lessons. Those measured the *generator* in isolation and
said nothing about whether a real paper could be turned into a usable bank. This
run starts from a PDF and goes all the way through:

    oex101_final_2024.pdf
      -> tools/extract/extract_pack.py     (pypdf, digital text)
      -> ai/clo_tagger.py                  (offline, qwen2.5:7b)
      -> POST /packs/load                  (live service, service credential)
      -> ai/quiz_generator.py              (live generation)
      -> eval/feature_b_rubric.py          (scoring)

### Environment

| | |
|---|---|
| Model | `ollama_chat/qwen2.5:7b` (local, CPU — `size_vram: 0`) |
| Provider base | `http://localhost:11434` |
| Pack | `oex101_final_2024.pdf`, sha256 `55dc1790c9c1c5ee…` |
| Offering | `course-v1:OpenedX+OEX101+2023` |
| Index | 55 active chunks (the real OEX101 course) |
| Bank | **5 questions, 4 tagged, 1 untagged** · 35 marks total |
| Command | `python eval/run_generation_eval.py --index <copy> --pack <extracted> --delay 0` |

### Results — n = 4

| Metric | Result | n | Kind |
|---|---|---|---|
| **clo_alignment** | **1.000** | 4 | quality — reads generated text |
| **band_plausibility** | **not run** | 0 | quality — see below |
| **not_a_duplicate** | **1.000** | 4 | quality — reads generated text |
| labelled_and_sourced | 1.000 | 4 | safety invariant |
| metadata_in_range | 1.000 | 4 | safety invariant |

**4 of 4 tagged questions generated.** The fifth was left untagged by the tagger
and is therefore not a source — counted and skipped, never silently dropped.

**Abstention, both negative cases:**

| Case | Outcome |
|---|---|
| no matching past question | **abstained** |
| unsupported outcome | **abstained** |

### Latency (generator only)

| Stage | median | p95 |
|---|---|---|
| source retrieval | 0.001 s | 0.002 s |
| context retrieval | 0.031 s | 0.038 s |
| duplicate check | 0.000 s | 0.000 s |
| validation | 0.000 s | 0.000 s |
| **time to first token** | **9.7 s** | **106.3 s** |
| total | 9.7 s | 106.3 s |

Same shape as §3.3: **retrieval is ~300× cheaper than inference**, and CPU
inference misses the 2 s design budget by 5×–53×. At n=4 the p95 is effectively
the maximum, so read it as "the slowest of four", not as a percentile.

### Why `band_plausibility` did not run, and why that is the honest result

It needs a requested difficulty band, and `extract_pack.py` **deliberately leaves
`difficulty` unset**: §7.6 requires a derived difficulty to be labelled derived
wherever it appears, so the extractor does not guess one. The authored pack
carried a difficulty on every question, which is exactly why it could report this
metric and a real pack cannot.

So the earlier `band_plausibility 0.882` was a measurement of the *authored*
pack's metadata, not of anything the extraction pipeline produces. Deriving
difficulty at extraction time is the work that would make this measurable
end to end; until then, the honest entry is "not run".

### What this run does not say

1. **n = 4.** One paper, five questions, four usable. This demonstrates the
   pipeline runs end to end and produces rubric-passing output; it does not
   establish a rate. The authored-pack run (n = 18/16/18) remains the larger
   sample, and the two must not be averaged.
2. **`not_a_duplicate` is token overlap against a 5-question bank.** A small bank
   makes the check easier to pass, not harder.
3. **The eval harness disables enrollment enforcement** (`ENFORCE_ENROLLMENT=false`)
   so it can run offline. Enrollment was verified separately, in a real browser
   against the live LMS — see §3.7.
4. **One rater, one course, one model**, unchanged from §6.

---

## 3.7 Real-browser verification — added 2026-08-12

The first end-to-end run through an actual browser, as an actual enrolled
student, against the live stack. Not an HTTP simulation.

| Check | Result | Evidence |
|---|---|---|
| Exam prep tab renders | **PASS** | live `/status`: "5 past-paper questions · 3 learning outcomes · 2024–2024" |
| 100-mark study plan | **PASS** | "Study plan — 20 of 100 marks", 2 outcomes, real question ids, 80 marks reported unfilled |
| Practice question, CLO with data | **PASS** | generated, badged, cited to the paper + 3 real lessons |
| Abstention, CLO with no data | **PASS** | "There isn't enough in this course's material to plan that reliably." |

Environment: `cm_student`, non-staff, real `CourseEnrollment` in
`course-v1:OpenedX+OEX101+2023`; token minted by the XBlock's own
`mint_student_token`; Tutor 21.0.8.

**The architecture held under a real browser.** The network trace shows
`handler/mint` → 200 on the XBlock, then the browser calling
`/coursemate/api/examprep/*` **directly** through Caddy — no LMS worker in the
answer path (invariant 1). SSE headers survived the reverse proxy:
`X-Accel-Buffering: no`, `Content-Type: text/event-stream`, chunked.

**Cross-offering was refused live**: the same student against DemoX returned
`403 not_enrolled` from the real enrollment check, not from a stub.

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

**Feature B end to end from a real PDF (§3.6):**

```bash
# 1. extract and tag the paper (offline, no student waiting)
python tools/extract/extract_pack.py tools/fixtures/oex101_final_2024.pdf \
    --offering course-v1:OpenedX+OEX101+2023 --exam-type final --year 2024 -o pack.json
python tools/extract/tag_pack.py pack.json --clos clos.json -o tagged.json

# 2. score the whole pipeline against a COPY of the live index
docker cp tutor_local-coursemate-1:/data/coursemate-index.db ./_live_index.db
COURSEMATE_STRONG_MODEL=ollama_chat/qwen2.5:7b \
COURSEMATE_MODEL_API_BASE=http://localhost:11434 \
python eval/run_generation_eval.py --index ./_live_index.db --pack tagged.json --delay 0
```

`--delay 0` because a local model has no rate limit; the 13 s default paces a
hosted free tier. Omit `--pack` to score the authored bank instead — the two
measure different things and must not be averaged.

**Agent regression gates** (no provider needed): `make agent-eval`.

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
5. **The real-PDF Feature B run is n = 4** (§3.6) and demonstrates that the
   pipeline works end to end, not how often it works. It is a smaller sample than
   the authored-pack run, not a replacement for it.
6. **`band_plausibility` is unmeasured on real extractions** (§3.6), because the
   extractor does not derive difficulty. Deriving it is the work that would close
   this, and until then the metric is absent rather than assumed.
7. **Tool-selection accuracy for the agent is still NOT MEASURED, and a `--live`
   attempt on 2026-08-12 did not change that.** `make agent-eval` scores the four
   loop gates against a scripted router, which measures them exactly. The `--live`
   run against `qwen2.5:7b` on CPU printed `0.44`, but **nine of its planning
   calls timed out** at 300 s first — so that figure measures timeouts, not tool
   choice, and is deliberately not recorded as a result. It is consistent with the
   earlier profiling finding that this model cannot drive the agent loop (it
   emits no parallel tool calls and took 145 s of a 222 s turn just to plan).
   Measuring this needs a hosted provider, which is why `agent_enabled` ships
   `False`.
