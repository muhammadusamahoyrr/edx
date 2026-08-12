# CourseMate — Technical Summary

*Written for a technical interview: what was built, the decisions that mattered,
what they cost, and what went wrong. The failures are the interesting part.*

---

## The problem

Bolting ChatGPT onto a course produces a tutor that is confidently wrong about
that course. It answers from training data, cites nothing, and cannot tell a
student "this isn't covered here." For a university that is worse than no tutor —
a student cannot distinguish a fabricated answer from a real one.

**CourseMate answers only from the course it lives in, cites the lesson, and
abstains when the course doesn't cover the question.**

Built on Open edX Ulmo, verified against two courses — 282 indexed chunks — on a
live stack with a Celery worker and a nightly sweep container.

---

## Architecture in 60 seconds

```
Browser ──1── XBlock.mint() → JWT (0.115 ms, LMS released)
   └────2──── /coursemate/* → Caddy → FastAPI service
                                        ├── boundary: identity → enrollment → filter → audit
                                        ├── query: reconstruct a follow-up from the conversation
                                        ├── FTS5 retrieval → rerank
                                        ├── first-turn cache? → replay, no provider call
                                        ├── confidence gate → abstain, no provider call
                                        ├── daily token ceiling → refuse, no provider call
                                        └── LiteLLM → SSE tokens → browser
   └────3──── persist turn → Scope.user_state (platform owns chat history)
```

Three packages with **disjoint dependency sets**: contracts (pydantic only), the
Open edX plugin (no AI libraries — enforced in CI), and the service (everything
expensive).

---

## Five decisions worth defending

### 1. The LMS is never in the answer path

**The obvious design is wrong.** Proxying the stream through the XBlock holds a
gunicorn worker for the whole generation — 5–15 seconds. It uses no CPU, but a
worker pool is exhausted by **occupancy**, not computation. Two hundred concurrent
students is two hundred occupied workers, and courseware rendering shares that
pool: the LMS goes down for students who never opened the tutor.

So the XBlock mints a JWT and returns; the browser streams from the service over a
same-origin path routed at Caddy.

**Measured:** 3 concurrent 4-second generations → **0 LMS log lines**, 103 ms LMS
CPU against a **118 ms idle baseline**. Streaming cost less than background noise.

*The lesson underneath:* my earlier reasoning said "the XBlock holds no work" —
true of computation, false of connections. **A guarantee stated in the wrong unit
reads as satisfied when it isn't.**

### 2. SQLite FTS5 instead of a vector database

No embedding provider was available. Rather than stall, I shipped the lexical half
of the hybrid the design already called for.

What it bought: no new infrastructure (stdlib), **deterministic** results — so a
retrieval change is attributable rather than lost in sampling noise — and a
swappable implementation behind `ContextProvider`.

What it costs, stated: paraphrase fails. *"What causes processes to hang
forever?"* won't find "deadlock". That is the top item in the limitations doc, not
a footnote.

**The trade I'd defend:** at 231 chunks, a vector database is a container, a
client and a failure mode in exchange for nothing measurable. Retrieval runs in
**12 ms p95**; inference takes 24 s. Optimising retrieval further would be
optimising 0.05% of the latency.

### 3. Authorization is re-derived, not trusted

A signed, unexpired, correctly-scoped JWT is **not sufficient**. The signature
proves the token was issued; it does not prove the enrollment still holds. Unenroll
a student and their token works until expiry.

The boundary asks Open edX on every call, cached 60 s, invalidated immediately by
the LMS unenrollment receiver, and **fails closed** — if the platform is
unreachable, access is denied. An availability problem must never become an
authorization bypass: a tutor that is down is recoverable; one serving another
cohort's content is not.

Verified: `admin` → allowed; `nosuchuser` → denied; platform down → denied.

### 4. Architecture enforced in CI, not in review

Six `.importlinter` contracts. The most valuable forbids the Open edX plugin from
importing any AI library — that is what makes *"CourseMate cannot degrade your
LMS"* structurally true rather than aspirational.

**I verified the contract can fail** by adding a deliberate `import litellm`: exit
1, exact import named. A green contract that cannot fail is decoration.

It also caught *me* twice: my reindex command imported `modulestore` directly, and
an early version of contract 1 forbade `tasks → content_adapter → xmodule` —
**the very architecture it was defending.** Contracts 1 and 3 are now scoped to
direct imports; contract 2 stays transitive, because an indirect import still
drags the library into the image.

### 5. Evaluation before optimisation

I built the benchmark before adding retrieval techniques, and it immediately
justified itself.

Metric choices worth defending: **recall leads** (a generator can ignore an
irrelevant chunk but cannot invent a missing one); **abstention measured in both
directions** (tuning only against false answers yields a tutor that refuses
everything and scores perfectly); **no LLM-as-judge** — a judge that drifts cannot
tell you whether your retrieval changed or the judge did.

---

## Results

| | |
|---|---|
| recall@3 (original arm / paraphrase arm) | **1.000** / **0.300** |
| MRR (reranker off → on) | 0.644 → **0.833** |
| Retrieval p95 | **12 ms** |
| False-answer rate | **0.000** (was 1.000) |
| Citation correctness | 1.000 |
| Authorization matrix | 4/4 |
| Agent regression gates | **4/4** (n=10 scenarios; measures the loop, not the model) |
| Tool-selection accuracy | **NOT MEASURED** — needs a hosted provider; see below |
| Feature B, real PDF → scored question | CLO alignment **1.000**, duplicate-free **1.000** (**n=4**) |
| Feature B band plausibility | **not measured** on extracted packs — the extractor does not derive difficulty |
| Multi-turn retrieval recall@3 | **0.917** (was 0.333); answered-from-wrong-lesson **7 → 1** of 12 |
| First-turn cache, real browser | **74,973 ms → 133 ms**, 0 tokens charged on the hit |
| Coverage (service + contracts) | **90.4%**, gated at 80%. Platform 26%, reported ungated — most of it needs Open edX to execute |
| Tests / contracts | 736 backend + 63 browser / 6 |

**Tool-selection accuracy was attempted against a real model on 2026-08-12 and
still is not a number.** The local `qwen2.5:7b` timed out on nine of ten planning
calls before producing a score, so what it printed measured timeouts rather than
tool choice. It is recorded as unmeasured rather than reported.

---

## The four bugs that matter

**Every one was found by tooling or measurement. Three were invisible in normal
use. Two looked like success.**

### The confidence gate could never fire

```python
best = min(r["raw"] for r in rows)
score = raw / best          # the top hit is ALWAYS exactly 1.0
```

Normalised against the best row *of the same query*, so the threshold was
unreachable while any row came back. The tutor answered *"explain quantum
chromodynamics"* from an unrelated lesson.

**It presented as success:** groundedness 1.000, hallucination 0.000 — because the
model faithfully grounded its answer in the irrelevant chunk it was handed.
Generation metrics alone would have called it a win. This is exactly why the
benchmark scores retrieval separately.

**Root cause: a relative quantity used as an absolute threshold.** BM25 magnitude
is corpus-dependent — excellent for ranking, meaningless as confidence. Now BM25
orders and query-term coverage gates.

### 88% of the course silently unindexed

Each ingest batch ran its own write→verify→swap, and swap deactivates all other
versions. A 226-block course served **26 blocks while every batch reported
success.** Nothing errored. Fixed by making the run, not the batch, the swap unit.

### A settings import that took down the platform

`settings/common.py` read `ENV_TOKENS`, which doesn't exist at COMMON stage —
raising during Django startup. My plugin would have stopped the LMS booting **for
every course on the instance**, including those that never enabled CourseMate. A
second version raised on an unset key, with the same effect.

Found by installing into a running platform rather than by review — and by
choosing a fast in-container install over a 45-minute image rebuild, which would
have baked it in.

### FTS5 injection through a student's question

Stripping punctuation left FTS5 keywords intact, so `cats AND dogs` parsed as
operators and raised. A student could 500 the endpoint by typing "OR".

---

## Challenges

**A slow, unstable network reshaped the tooling.** Pulls stalled with live TCP
connections and zero bytes; PyPI read timeouts surfaced as a misleading *"no
matching distribution for setuptools"*. The fix was structural: split the
Dockerfile so dependencies form a cached layer, then snapshot them as a base
image. Source rebuilds went from 10–15 minutes and frequently failing to **seconds
and offline**.

**Windows/WSL2 cost most of a day.** `wsl --install` hung silently; port 80 was
held by WSL's own stale relay; the distro was torn down between commands, killing
Docker before the LMS could finish booting.

**Undocumented platform behaviour.** The demo course key is
`course-v1:OpenedX+DemoX+DemoCourse`, not the `edX+DemoX+Demo_Course` in almost
all documentation. `X-Edx-Api-Key` returns 401 on current Open edX.
`generate_course_blocks` reports success while changing nothing.

---

## Lessons

**1. Absence of a signal is not a negative result.** I measured a nonexistent
network interface, got "0 KB", and declared a working download stalled. A
leftover test server answered 200 and nearly went into a report as success. A
background task exited 0 having done nothing. *An instrument you haven't validated
is not evidence.*

**2. Metrics that only measure the output hide the failure.** Groundedness 1.000
while answering every off-topic question. Splitting retrieval from generation is
what surfaced it.

**3. Guarantees must be stated in the unit that runs out.** "Holds no work" was
true of CPU and false of connections.

**4. Make guard rails fail, then trust them.** Both the architecture contracts and
the hostile-input tests caught real bugs — but only after I verified they *could*
fail.

**5. Say what isn't done.** An unwired cache with passing tests read as an active
security control. Its README now says "not wired" in the first line. The
limitations document exists for the same reason.

---

## What I'd do next

1. **Redis for the rate limiter and authz cache** — both are per-process and fail
   silently on a second replica.
2. **Add embeddings as a second retriever**, merged with BM25 (hybrid retrieval).

---

## Honest scope

The distinction that matters in what follows: **implemented** means the code
exists and unit tests cover it; **verified** means it was observed working on the
live stack; **measured** means a number came out of an executable run.

**Implemented, verified on the live stack, and measured:** grounded tutoring with
citations, abstention, enrollment-enforced retrieval over real course content,
streaming that never occupies an LMS worker, and a benchmark that found four real
bugs.

**Feature B — implemented and verified in a real browser (2026-08-12).** A real
past-paper PDF is extracted (`pypdf`, digital text), CLO-tagged offline, loaded
through the service-credentialed endpoint, and served to an enrolled student:
budgeted study plan, generated practice question with provenance, and correct
abstention on an outcome with no source material. Measured end to end at **n=4**
— a demonstration that the pipeline works, not a rate.

**Implemented but shipping dark:** the agent layer. `agent_enabled` defaults to
`False` and the deterministic planner answers instead. The loop's failure rules
are measured (4/4 gates); which tool a real model picks is not.

**Not built:** semantic retrieval, OCR/VLM extraction for scanned papers,
difficulty derivation at extraction time, the instructor loop, multi-replica
operation.

**Not production-deployed.** The nearest gaps are operational rather than
architectural — the boundaries held across six phases, and the seams built for
retrieval and model swapping both absorbed real replacements without the API
moving.
