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

Started as 18 questions built by **sampling the actual index**, not invented — a
gold set written without looking at the corpus measures the author's
imagination. 12 covered, 6 uncovered (4 clearly off-topic, 2 adversarial:
plausible-sounding platform questions absent from this course).

It has grown twice since, and the arms are **not interchangeable**. Each was
added to measure something the previous ones could not:

| Arm | n | Added | Measures |
|---|---|---|---|
| `original` | 18 | initial | retrieval when the question shares the lesson's words |
| `paraphrase` | 10 | 2026-08-05 | retrieval when it deliberately does not (§3.5) |
| `multiturn` | 12 | 2026-08-12 | a follow-up that cannot be searched alone (§3.8) |
| `topic_change` | 4 | 2026-08-12 | a self-contained question *after* a conversation (§3.8) |
| `usage_key_conflict` | 2 | 2026-08-12 | page context disagreeing with the conversation — **carried, not yet a target** |
| **total** | **46** | | 40 covered, 6 uncovered |

**The headline retrieval figures in §3.1 cover the single-turn arms only**
(`original` + `paraphrase`, n = 28 of which 22 covered). That is what they
measured before the conversational cases existed, and averaging the new arms in
would have silently turned "retrieval quality" into a blend of retrieval quality
and an unfixed conversational defect. `run_eval.py` reports per arm; a blend of
arms is not a measurement of anything.

**n is small in every arm.** Every figure below should be read with that
attached.

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

## 3.8 The conversational arms — added 2026-08-12

Until this point the retriever was given `request.question` and nothing else. The
conversation reached the **model** through `history` and never reached the
**retriever**, so a follow-up like *"why would I use one?"* was searched with no
idea what "one" referred to.

Measured against the live DemoX index, before any change:

| Arm | n | recall@3 | Answered from the **wrong** lesson |
|---|---|---|---|
| `multiturn` | 12 | **0.333** | **7 of 12** |
| `topic_change` | 4 | 0.750 | 1 |

The second column is the one that matters. Those seven were not abstentions — the
blended rerank score cleared τ = 0.35, so the pipeline answered fluently, with a
citation, from the wrong lesson. The sharpest case scored **1.000**: *"How do I
try it out?"* after *"What is Studio?"* matched a block literally named
`Try it -`. Maximum confidence, wrong content.

### The fix, in two measured stages

**B1** prepended the previous student turn to every query. Multi-turn recovered
and `topic_change` regressed — recall@1 fell **0.750 → 0.250**, because the
previous subject's terms competed with a question that never needed them. A
correct block still retrieved, just no longer first.

**B2** made the reconstruction conditional on the question actually being
under-specified — a closed set of pro-forms (`it`, `one`, `this`, `them`, …).
Pronouns only, deliberately: it is a finite grammatical class, so the list cannot
quietly become a lookup table for the eval set.

| Arm | n | recall@3 before | recall@3 after | wrong-and-answered |
|---|---|---|---|---|
| `original` | 18 | 1.000 | **1.000** | unchanged |
| `paraphrase` | 10 | 0.300 | **0.300** | unchanged |
| `multiturn` | 12 | 0.333 | **0.917** | **7 → 1** |
| `topic_change` | 4 | 0.750 | **0.750** | 1 |

**0.333 → 0.917 on multi-turn, with both single-turn arms held exactly.** The one
remaining multi-turn miss (`m05`, *"Can you give an example?"*) is
under-specified by **ellipsis** rather than anaphora — "an example *of what*" —
and contains no pronoun to find. Catching it needs to know which nouns are
topical in this corpus, which is a different signal and is not attempted.

**No LLM query rewriter, deliberately.** The standard answer is to ask a model to
rewrite the follow-up into a standalone question. That puts a model call *before*
the confidence gate and destroys the property the system is built on: abstention
costs ~3 ms and no spend. On this CPU model it would add ~25 s to every question
including the ones we refuse.

### What the offline numbers missed

**B1/B2 passed every test and were a complete no-op in production.** `tutor.js`
pushes the question onto its history array *before* building the request, so the
wire payload carries the question in `history` **and** in `question`. The
"previous" turn was the current question echoed back, and the reconstructed query
came out as `"Why would I use one? Why would I use one?"`.

The unit tests and the offline harness both build history the way the *contract
reads* — prior turns only. Production sends a different shape. Verified in a real
browser before and after:

| | turn 2 citations after *"What is a cohort?"* → *"Why would I use one?"* |
|---|---|
| before | `Design a Logic Gate`, `Content Groups` |
| after | `Setting up Cohorts`, `Cohorts, Content Groups, and Components`, `Content Groups` |

The regression tests for the fix use the **verbatim payload captured off the
wire**, not a hand-written one. See §4.5.

---

## 3.9 Per-student spend ceiling (C1) — added 2026-08-12

Rate limiting caps how *often* a student asks; the concurrency limit caps how
many streams they hold open. Neither bounds total spend — 20 questions a minute
all day is inside both.

| | |
|---|---|
| Ceiling | **100,000 tokens** per student, per course, per **UTC** day |
| Setting | `COURSEMATE_STUDENT_DAILY_TOKEN_BUDGET` (0 or less disables) |
| Redis key | `cm:budget:{offering_id}:{student_id}:{YYYYMMDD}`, TTL 48 h |
| Enforced | before the provider call, after the confidence gate |
| Unit | tokens, not dollars |

**Tokens, not dollars**, because a price table is wrong the moment a provider
reprices or the router falls back to another deployment. The date in the key
rolls the budget over; the TTL only reclaims memory.

**Placed after the gate, deliberately.** Retrieval and the gate are local and
free, so a student who is out of budget and asks something the course does not
cover still gets `abstained` — the true answer, which was never going to be
charged.

### Provider usage vs. the estimate — measured live

Usage is taken from what the provider reports. **This deployment's provider
reports nothing.** Verified 2026-08-12 by probing the running router directly:
`ollama_chat/qwen2.5:7b` returned three chunks with `usage=None` on every one and
`total_tokens` never present.

So **production charges the estimate**, a token count over the actual prompt and
the actual generated text. Confirmed by arithmetic as well as by the probe: for
one real browser question the prompt rebuilt to 4,759 chars + a 423-char answer =
5,182 chars → `5182 // 4 = 1295`, matching the observed ledger delta **exactly**.

That fallback exists because the alternative — charging nothing when a provider
is silent — would make an unmetered tutor out of a deployment detail. It is an
estimate and is named one; it will be wrong in the third significant figure and
the ceiling has enough headroom that this does not matter.

**What is never charged:** an abstention (no provider call), or a provider that
failed before emitting a token. A failure *after* partial output **is** charged —
those tokens exist on the bill and the student read them.

### Measured cost per answer

Observed ledger deltas across seven real generations on 2026-08-12:

| Shape | Tokens charged |
|---|---|
| first turn, no history | 660 – 785 |
| short conversation (3 turns) | ~908 |
| long conversation (9 turns) | 1,295 |

So **roughly 75–150 answers per student per course per day**, depending on how
long the conversation has grown. Production runs `max_output_tokens = 400`.

### Redis failure

Degrades to **per-process counting**, not fail-open and not fail-closed. Failing
closed turns a cache outage into a total tutor outage — the trade `shared_state`
already rejected for the rate limiter. Per-process is bounded: worst case during
an outage is `replicas × ceiling` rather than unlimited, and exactly the ceiling
on this single-replica deployment. Pinned by
`test_budget_ledger.py::test_a_redis_outage_is_not_unlimited_spending`.

---

## 3.10 First-turn response cache (C2) — added 2026-08-12

The tutor's cost is one ~55 s provider call; retrieval is ~3 ms of local SQLite.
So the only cache worth having skips generation, and the only questions safe to
skip it for are the ones whose answer depends on nothing but the question.

| | |
|---|---|
| Scope | **first turn only** (`history` empty after normalisation) |
| Key | `resp:` + sha256[:32] over tenant, offering, index version, effective scope, filters, normalised query, mode |
| Invalidated by | a new **index version** (any reindex), or the TTL |
| TTL | 3,600 s |
| Cached | successful answers **and** abstentions |
| Not cached | `preparing`, degraded (fallback-deployment) answers, anything touching a personal namespace |

**Isolation is the point, not the speed.** §10.2 calls response caching the place
isolation quietly fails *after* every filter is written correctly. The key
carries the caller's effective scope — student, roles, offering — and the
`group_tokens` the block-level access filter runs on. Verified live: two students
in this deployment genuinely do carry group tokens (`cm_student` mints
`["50:1"]`), so that component is doing real work, not just passing a test.

**Authorization cannot be bypassed, structurally.** The read sits *after*
retrieval, which is what enforces enrollment, applies the group filter and writes
the audit record. A denied caller's retrieval returns `index_version=None`, and
the pipeline only builds a key when it has a version — so there is no ordering in
which a cache hit can precede the enrollment check.

### Measured live, in a real browser

Same question, twice, each as a genuinely fresh first turn (history cleared
between them, 0 rendered turns before each):

| | request 1 | request 2 |
|---|---|---|
| total | **74,973 ms** | **133 ms** |
| time to first token | 51,561 ms | 130 ms |
| token frames | 78 (streamed) | 1 (replayed) |
| citations | Content Groups, Content groups, Cohorts Content Groups and Components | **identical** |
| answer | 445 chars | **byte-identical** |
| budget charged | 785 | **0** |

**564× faster, and the provider was not called** — the zero budget delta is the
proof, since C1 charges every generation.

A multi-turn request for the same question (history `[student, tutor, student]`)
took 35,958 ms, streamed 78 frames and charged 908: it generated, as it must.
A request with a different `group_tokens` scope took 33,110 ms and charged 811 —
it did not read the other scope's entry.

### The browser-history normalisation

**The same defect as B1/B2, and it made C2 unreachable.** The first
implementation asked `not request.history`. `tutor.js` pushes the question into
`history` before building the request, so the browser never sends an empty one —
not even on a student's first question in a block. Live verification on a freshly
cleared block: a full 50-second generation and `resp:*` still zero.

`is_cacheable_request` now strips a trailing **student** turn whose content
equals the question, then checks whether anything remains:

```
[]                                        -> first turn
[student "Q"]                  (browser)  -> first turn
[student "P"]                             -> NOT first turn
[student "P", tutor "A", student "Q"]     -> NOT first turn
```

By role and content, not by position — the browser sends the echo on *every*
turn, so a positional rule would make every follow-up look like a first turn and
the cache would start serving one student's conversation to another.

**Hit rate is low by design, and this is an honest limitation.** `student` is in
the key, so a hit needs the same student to ask the same question as a first turn
twice — and their history persists between attempts. Sharing across students with
identical scope would be a large win and is defensible, since retrieval is
scope-determined; it is a security trade that has not been made.

---

## 3.11 Two providers, compared and failed over — added 2026-08-14

Until this date CourseMate ran **one provider and one model**: both logical tiers
pointed at local `qwen2.5:7b`. The fallback chain, the `DEGRADED` frame and
`provider_failures_total` all existed and had never executed against a real
outage. §2 of `LIMITATIONS.md` said so.

The live topology is now `strong` → OpenRouter (hosted), `cheap` → local Ollama.
`fallback` remains unset, so the chat chain is `strong → cheap`.

### Same question, same context, pinned deployments

`eval/run_model_comparison.py`, n=2 questions. Retrieval runs **once per
question** and the identical message list goes to every deployment — otherwise a
retrieval difference reads as a model difference. Each call pins its deployment
by name **and passes `fallbacks=[]`**, so a failing pin cannot be silently
answered by another deployment and recorded under the wrong name.

| deployment | answered | median total | median first token | median chars | unsupported |
|---|---|---|---|---|---|
| `strong` — `openrouter/meta-llama/llama-3.3-70b-instruct` | 2/2 | **4,316 ms** | 3,512 ms | 335 | **0** |
| `cheap` — `ollama_chat/qwen2.5:7b` | 2/2 | **49,120 ms** | 1,039 ms | 811 | 1 |

Per question:

| q | deployment | total | first token | chars | unsupported |
|---|---|---|---|---|---|
| q01 *What are video transcripts used for?* | `strong` | 5,175 ms | 5,148 ms | 255 | 0 |
| | `cheap` | 33,877 ms | 1,154 ms | 592 | 1 |
| q02 *How do I set up cohorts in my course?* | `strong` | 3,457 ms | 1,876 ms | 415 | 0 |
| | `cheap` | 64,362 ms | 924 ms | 1,030 | 0 |

**The local model starts faster and finishes far slower** — 1,039 ms to first
token against 3,512 ms, then ~11× the total. For a streaming UI that inversion
matters more than the totals suggest.

### Three caveats, without which these numbers mislead

1. **Latency compares hardware, not models.** A hosted GPU against local CPU
   inference is not a property of either model. Nothing here supports "llama-3.3
   is faster than qwen2.5".
2. **Free-tier throttling inflates the hosted column**, unpredictably and in the
   opposite direction to (1).
3. **Token counts are absent for both.** `ollama_chat` reports no usage on stream
   chunks (§4.1 of `LIMITATIONS.md`), and OpenRouter reported none either in this
   run. The report renders `—`, never `0`: a zero would claim the model spent no
   tokens.

### How much difference is meaningful — a variance floor

Run immediately before, with **both deployments pointing at the same local
model**, as a control:

| deployment | model | median total | median first token | chars | unsupported |
|---|---|---|---|---|---|
| `strong` | `ollama_chat/qwen2.5:7b` | 120,958 ms | 78,089 ms | 786 | 2 |
| `cheap` | `ollama_chat/qwen2.5:7b` | 37,686 ms | 1,101 ms | 660 | 0 |

**Same model, same context, same prompt — a 3× latency spread and 2 vs 0
unsupported sentences.** That is the noise floor of local CPU inference under
contention, and it is the bar a claimed model difference has to clear. The
harness prints a warning when two deployments resolve to the same model, because
a table that looks like a comparison while comparing a model with itself is worse
than no table.

### Failover, against a real outage

`tools/verification/failover_probe.sh`. The probe builds its own Router inside
the service container from the deployed settings with one field overridden; the
tutor config, container env and uvicorn workers are untouched, so restoration is
guaranteed by the process exiting.

| step | answer | citations | DEGRADED | error | `provider_failures_total` |
|---|---|---|---|---|---|
| baseline | 255 chars | 3 | no | — | 0 |
| hosted key invalidated | 334 chars | **3** | **`qwen2.5:7b`** | — | **0** |
| hosted **and** local unreachable | 0 chars | 0 | no | `unavailable` | **+1** |
| restored | 255 chars | 3 | no | — | 0 |

Row 2 is the result that matters: **the hosted vendor was gone, the student still
received a cited answer from course material, and the UI was told it was
degraded.** Latency went 7,463 ms → 27,084 ms, which is the cost of the floor.

Row 3 is the honest failure. With `fallback` unset there is one hosted provider,
so disabling it *is* the "all hosted providers down" case; killing the local floor
as well proves the tutor refuses rather than fabricating.

**`provider_failures_total` did not move on the successful failover, and cannot.**
See §4.6 and `LIMITATIONS.md` — it counts generations that failed *entirely*, and
the Router swallows the provider failure whenever a fallback succeeds.

### Two defects in the probe itself, both found by running it

Recorded because both produced confident, wrong output first:

* **Wrong identity.** The first version used `username="probe"`. The boundary
  re-derives enrollment per call and fails closed, so every step abstained before
  the model was reached — a failover probe measuring authorization. Now uses the
  enrolled identity the eval harness uses.
* **The response cache served every step.** After the baseline populated it,
  steps 2–4 returned the *identical* 255-char answer in ~15 ms while the provider
  was unreachable. The cache working exactly as designed, and masking the entire
  experiment. The probe now disables it.

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

### 4.5 A hand-built fixture hid the same defect twice — 2026-08-12

Not a benchmark finding. **Both times it took a real browser to see it**, and
both times every offline test was green.

`tutor.js` pushes the student's question onto its history array *before*
building the request, so the wire payload carries the question in `history` as
well as in `question`. Two features shipped assuming otherwise:

1. **B1/B2** took the "previous student turn" and got the current question back,
   producing the query `"Why would I use one? Why would I use one?"` — a no-op
   dressed as a fix (§3.8).
2. **C2** tested `not request.history` for "first turn", which the browser can
   never satisfy. The cache was unreachable: a full 50-second generation on a
   freshly cleared block left `resp:*` at zero (§3.10).

The shared root cause is not the push. It is that **every fixture was written
from the contract rather than captured from the client**, and the contract says
`history` holds prior turns. A harness that constructs its own input cannot
discover what a real client actually sends.

Both fixes now carry regression tests built from the **verbatim payload captured
off the wire**, kept as raw JSON rather than rebuilt with `Turn(...)` — because
rebuilding it is precisely how the bug survived.

### 4.6 `provider_failures_total` cannot see a silent degradation — 2026-08-14

Found by Phase 4, by writing the assertion the brief asked for and watching it
fail for a reason that turned out to be correct.

The intended check was *"a failover emits DEGRADED **and** increments
`provider_failures_total`"*. Those two are **mutually exclusive**. The counter is
incremented in `pipeline.py` only inside the `except` blocks — and LiteLLM's
Router handles the fallback internally, so when a fallback **succeeds** no
exception ever reaches the pipeline.

Measured:

| scenario | DEGRADED | `provider_failures_total` |
|---|---|---|
| primary down, fallback answers | yes | **+0** |
| whole chain down | no (ERROR instead) | **+1** |

So the counter means *"generations that failed entirely"*, which is what its
description says — but its **name** says provider failures, and a primary that is
silently degrading every single request moves it by zero. The one condition an
operator most needs paged on is the one it cannot report.

Not fixed here, deliberately: the fix is a new counter
(`degraded_answers_total`, incremented where the DEGRADED frame is emitted), and
that is a behaviour change. Recorded in `LIMITATIONS.md` and `ADR-0001`.

---

## 5. Reproduction

```bash
# Two providers on identical context, deployments pinned (§3.11)
docker exec tutor_local-coursemate-1 python /eval/run_model_comparison.py --limit 2
#   → eval/reports/model_comparison_<UTC>.{json,md}

# The failover chain against a real outage (§3.11)
MSYS_NO_PATHCONV=1 tools/verification/failover_probe.sh

bash tools/verification/stage_eval.sh
docker exec tutor_local-coursemate-1 python /eval/run_eval.py --gen 6
# → /eval/reports/latest.json
```

Retrieval is measured on all 46 questions and reported per arm; generation is
sampled at 6 **from the single-turn arms only** — running the generator on a bare
follow-up like *"why would I use one?"* would measure how the model copes with a
question stripped of its conversation, which is not what the benchmark reports.
Full generation coverage costs ~12 minutes of CPU inference and, at this sample
size, would not change the conclusions.

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
3. **n=46 across five arms, single rater (the author).** Indicative, not settled.
   Read the per-arm n, not the total: `topic_change` is 4 cases and
   `usage_key_conflict` is 2, so neither carries a confident percentage.
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
