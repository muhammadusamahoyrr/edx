# CourseMate — Build Plan

*How the Built column of §1.2 gets built: sequence, milestones, and what gets sacrificed first if the window closes. Derived from `CourseMate_Complete_Design.md` v7 and `CourseMate_Repository_Structure.md`. Where this plan and the design disagree, the design wins and this document is stale.*

**Constraints, restated so the plan can be judged against them:** ~4 weeks, **one engineer**, no prior running instance, and a deliverable that is a *demo plus published numbers*, not a production rollout. That combination — solo, short, demo-terminal — determines almost every sequencing choice below.

---

## 0. Five rules this plan is sequenced by

1. **Burn the variance first, not the interest.** The tasks that can blow the schedule are environmental and unverified, not algorithmic. Retrieval quality is *tuning*; a Tutor instance that won't load a plugin is a *wall*. Everything that could invalidate a week of work happens on days 1–2.
2. **A walking skeleton before any depth.** One thin end-to-end path — publish a block → it is indexed → ask in a lesson → get a cited answer — exists by **end of week 2**. Every later day thickens a path that already works. The alternative (build each layer well, integrate at the end) fails catastrophically for a solo demo deliverable, because integration risk lands in the week with no slack.
3. **Decouple from the platform wherever possible.** The service is buildable against a JSON fixture of course blocks with no Open edX at all. So a bad day in Tutor never blocks service work — it reorders it. This is the only "parallelism" available to one engineer, and it is worth designing for.
4. **Demoable at the end of every week.** Not "code complete" — *runnable in front of someone*. A week that ends with nothing to show is a week whose progress can't be checked, including by me.
5. **The cut ladder is the buffer** (§6). There is no float in a solo four-week plan; pretending otherwise is how §1.2's discipline gets abandoned under pressure at exactly the moment it matters. So what gets cut, and in what order, is decided *now*, while it is a design judgement rather than a panic.

---

## 1. Day 1–2: risk burn-down

Nothing on the critical path gets built until these are answered. Five items, one and a half days, hard stop — if any overruns, it becomes a scope decision that day rather than a quiet delay.

| # | Spike | Timebox | Why it goes first | If it fails |
|---|---|---|---|---|
| **S1** | Tutor up; empty plugin installs into **LMS, CMS and worker**; `plugin_app` registers receivers in both; hello-world XBlock renders in a unit | 1 day | Highest-variance task in the project and everything depends on it. Also the first real test of the `lms.djangoapp`/`cms.djangoapp` split | Escalate immediately — this is a schedule event, not a bug |
| **S2** | **Ingress: can the service be served at `/coursemate/` under the LMS origin?** (§2) | ½ day | The mint-and-connect decision is now design (§3.4 r3, v8); what's left is proving the routing works in Tutor | Non-streaming single response through the XBlock, accepting bounded worker occupancy; latency claim narrows |
| **S3** | Verification tests 1, 3, 4, 5 from §3.6 | ½ day | Each gates a design choice; test 2 is already closed from source (v7) | Consequences are pre-written in `Week1_Verification_Plan.md` |
| **S4** | Vector store choice → **ADR-001** | ½ day | §6.3's filter-before-rank isolation is much simpler in a store that filters and ranks together | Default to the option with the strongest metadata filtering, not the best vectors |
| **S5** | Cross-encoder on CPU: model size, cold start, memory, latency for 20 pairs | 1 hour | §8.2 budgets 250 ms; if the container needs 4 GB that's a deployment fact | Skip rerank in MVP; §8.2 already defines the degradation |

S1 and S3 share an instance, so they interleave. Total ≈ 2 days including setup.

---

## 2. S2 in full: the streaming proxy occupies an LMS worker

> **Status: adopted into the design as v8, §3.4 rule 3.** What follows is the reasoning that produced it, kept here because it is the argument the build plan is sequenced around. S2 is no longer *"decide the shape"* — it is *"prove the ingress routing works on Tutor,"* which is half a day of nginx/Caddy configuration rather than an architectural question.

**The mechanical question.** `@XBlock.json_handler` returns a dict, serialized to JSON — it cannot stream. `@XBlock.handler` gives a raw `webob` request/response, and webob supports `app_iter`. But the LMS routes handler calls through `handle_xblock_callback`, which converts the webob response into a Django response — and whether streaming survives that conversion is **unverified**. That alone justifies the spike.

**The architectural question, which matters more.** §3.4 puts the XBlock in the LMS as a thin proxy specifically so that *"a class of 200 using the tutor during a lecture"* cannot exhaust the gunicorn pool. But **a streaming proxy holds the connection open for the entire generation** — 5–15 seconds per answer. It holds no *CPU*, but it holds a *worker*, and gunicorn's pool is exhausted by occupancy, not by computation. Two hundred concurrent streams is two hundred occupied workers. The failure mode §3.4 was written to prevent returns through the door the fix opened.

**Recommendation — the XBlock mints, the browser connects.** The design already has the JWT (§3.4); it only assumed the proxy shape around it.

```
XBlock json_handler  →  mint short-lived JWT, return it   (worker released in ~30 ms)
Browser  →  EventSource / fetch stream  →  CourseMate service   (no LMS worker involved)
```

This preserves §3.4's *actual* goal far better than proxying does, and it costs the LMS one fast handler call per conversation instead of one held worker per answer.

**The trade it introduces, stated honestly:** the service must be reachable from the browser, which appears to contradict §3.4's *"not internet-exposed, reachable only on the private network."* The resolution is an ingress path rather than a public service: expose the service **under the LMS domain at a path** (e.g. `https://lms.example.edu/coursemate/`), routed at the reverse proxy. Same-origin for the browser, no CORS, no separately published hostname, and **the traffic never enters an LMS application process** — which is the property that was actually being protected. The service still verifies the JWT and re-derives authorization on every request (§3.4), so browser-reachable does not mean unauthenticated.

**Decided on day 2, and now settled in the design.** Rate-limiting and the circuit breaker move from the XBlock to the service under this shape — a small change made up front, and a rewrite had it surfaced in week 3.

---

## 3. The critical path

Everything else is a leaf hanging off this chain. If a day slips, ask first whether it slipped on the chain.

```
S1 plugin loads in LMS+CMS+worker
   └─▶ content_adapter.iter_leaves (owns published_only, §3.6 v7)
         └─▶ contracts.ingest  ─────┐
                                     ├─▶ service: chunk → embed → write-verify-swap
         └─▶ bootstrap task ────────┘        │
                (+ thin command)             │
                                             ▼
                                      INDEX EXISTS  ◀── M1, end of week 1
                                             │
   S2 decides the surface shape              │
   └─▶ XBlock + JWT ──▶ service chat endpoint│
                          └─▶ boundary (authz·filter·audit)
                                └─▶ retrieve ─┘
                                      └─▶ LangGraph → LiteLLM → stream → cite
                                                    │
                                             ANSWER EXISTS ◀── M2, end of week 2
```

**Two things are deliberately *not* on the chain and must not be allowed to creep onto it:** query rewriting and the cross-encoder rerank. Both improve answers; neither is required for one to exist. They are week-2 polish precisely so they can be sacrificed (§6) without touching the skeleton.

---

## 4. Week by week

### Week 1 — foundations, and proof that content reaches an index

| Day | Work | Done when |
|---|---|---|
| 1 | **S1**. Repo skeleton, three packages, `.importlinter` wired into CI on day one | Hello-world XBlock renders in a unit; plugin in `INSTALLED_APPS` for LMS *and* CMS |
| 2 | **S2–S5**. Decisions recorded as ADR-001/002. Target release pinned | Every spike has an answer written down, not remembered |
| 3 | `contracts` v0. Service skeleton: health, ingest endpoint, docker-compose (service + vector store + metadata db + cache) | `POST /ingest/blocks` with a hand-written fixture puts a vector in the store |
| 4 | `content_adapter`: `get_block` / `iter_leaves` / `get_course_meta`, owning the branch context. Per-type extractors as S3/test-1 dictated | `iter_leaves` on a real course returns clean text for every supported type; unsupported types logged, not silently dropped |
| 5 | `tasks/bootstrap.py` + thin `coursemate_reindex` + resumable `course_index_state`. Service-side chunk → embed → write-verify-swap | **M1** |

> **M1 — content is indexed.** Run `coursemate_reindex --course <key>` on a real course; blocks land in the index; the final count reconciles against the course tree; killing the worker mid-run and re-running resumes rather than restarts.

**Why bootstrap before the event receiver**, when the event path is the "real" one: §5.1 names an empty index as *"the most likely way a demo fails,"* and the command is also the only way to get test data for every downstream day. The receiver is an optimisation on top of a working index; the command is the thing that makes weeks 2–4 possible.

### Week 2 — the walking skeleton becomes a tutor

| Day | Work | Done when |
|---|---|---|
| 6 | CMS receiver → Celery → resolve-to-leaves → dedup → POST. `XBLOCK_DELETED`. LMS receiver → invalidation | Publish a section in Studio; only changed leaves re-embed; delete a block, its chunks go |
| 7 | XBlock `student_view` chat UI + `json_handler` JWT mint + browser-side stream client; turn write-back to `user_state`. Service `chat` endpoint returning a canned answer | A student types in a lesson and a round trip completes end to end, with no LMS worker held for its duration |
| 8 | `boundary/`: `CourseIntelligence`, authz re-check, filters-before-ranking, audit. Semantic retrieval + metadata filters behind it | Two students in different courses cannot retrieve each other's content — tested, not assumed |
| 9 | LangGraph: retrieve → generate. LiteLLM. Streaming. Mandatory citation | A real question gets a real streamed answer citing a real block |
| 10 | Rewrite node (schema-constrained, cannot widen scope) + cross-encoder rerank | **M2** |

> **M2 — the tutor works.** *"What about that algorithm from week 4?"* asked inside a lesson returns a streamed, cited answer drawn from that course. **This is the demo.** Everything after this is guarantees, the second feature, and measurement.

**If week 2 ends without M2, that is the tripwire** — go to the cut ladder (§6) on the spot rather than absorbing it into week 3.

### Week 3 — the guarantees, Feature B, and deployment

| Day | Work | Done when |
|---|---|---|
| 11 | Confidence gate + abstention below τ; citation-required; the three distinct honest-failure states (`abstained` / `preparing` / `unavailable`). Query-time bootstrap backstop with in-flight lock + cooldown + ceiling | Asking about something not in the course gets *"not covered"*, not a fluent guess. An unindexed course says *"being prepared"* and enqueues once, not per refresh |
| 12 | Reconciliation sweep + `failed_ingestions` reporting. Studio view: config + **Index this course** + last-indexed + block count | Unpublish a unit, run the sweep, the tutor stops citing it |
| 13 | Exam prep: pack loader command, extraction routing, question records (§7.6), CLO tagging with human confirm at load time | A pre-loaded pack yields structured, filterable question records with provenance |
| 14 | Planner + quiz-generator nodes; marks budget; personal output labelled AI-generated with its source paper cited. Socratic mode | *"Prepare me for finals"* returns a CLO-organized plan and CLO-tagged practice |
| 15 | LiteLLM fallbacks + `RetryPolicy` + `allowed_fails`/`cooldown_time`; fallback tier visibly labelled. Deploy | **M3** |

> **M3 — both features work, deployed, and fail honestly.** Kill the primary provider mid-demo: the answer still arrives, labelled as a fallback. Kill both: it says so.

### Week 4 — measurement and delivery

| Day | Work | Done when |
|---|---|---|
| 16 | Eval harness; pilot set of 20–30 questions; Ragas across **retrieval and generation separately** (§11.1) | Four numbers exist, with the retrieval pair reported beside the generation pair |
| 17 | Abstention audit on a deliberately mixed set → initialize τ, report an interval not a point. Feature B rubric on 30 generated items | Both error directions measured; the rubric's four dimensions scored |
| 18 | Results written up with limitations beside them (single rater, named). Trace polish (§11.4) | A wrong answer can be localized to a stage |
| 19 | Demo recording, README, operator runbook, ADR tidy-up | **M4** |

> **M4 — assessable.** Numbers published against the definition of done that §1.3 committed to *before* building, with limitations printed next to them rather than in a footnote.

**19 days planned in a ~20-day window.** That is roughly one day of float, which is not a real buffer and should not be treated as one. The buffer is §6.

---

## 5. Testing, sized for four weeks

Not a test pyramid — a triage. What gets automated is decided by *what a failure would cost*, not by coverage.

| Tier | What | Runs |
|---|---|---|
| **`.importlinter`** | The five architectural contracts | Every commit, from day 1 |
| **Fast unit** (no platform, no network) | Chunking, cache key construction, JWT, rewrite schema validation, filter construction, locks | Every commit |
| **Isolation tests** — *the ones that must not be skipped* | Cross-student and cross-course retrieval; response-cache key includes permission scope; personal-namespace results never cached | Every commit |
| **Platform tests** (need Tutor) | Receivers fire, adapter reads published-only, command resumes | Manually, at milestones |
| **Eval harness** | §11 — quality, not correctness | Week 4, and on demand |

**The isolation tests are the ones worth defending under time pressure**, and they are cheap. §10.2 names caching as *"how isolation quietly fails after all the filters are written correctly"* — that is a bug class that passes code review, produces no error, and is discovered by a customer. Three tests, written the same day the cache lands.

Everything else can be manual. A solo four-week project does not earn a CI matrix.

---

## 6. The cut ladder — decided now, not under pressure

§1.2 drew the line between Built and Designed. This is the second line it needs: **the order in which Built items are sacrificed if the schedule closes.** Written now, while it's a design judgement.

**Two items from §1.2 constrain the ladder, and they are not negotiable:**

- **The reconciliation sweep cannot be cut.** §1.2 rule 2 — it is the only mitigation for unpublished content, and cutting it makes the Principle 3 exposure unbounded rather than one sweep interval.
- **The Feature B rubric cannot be cut** if Feature B ships. §11.3 — personal practice reaches students with no human gate, so measurement *is* the control. Cutting the rubric means shipping an unmeasured, ungated output.

Anything cut below is announced in the results, per §1.2 rule 3.

| Order | Cut | Costs | Why it's cuttable |
|---|---|---|---|
| 1 | **Socratic mode** | A demo talking point | A mode, not a guarantee. §8.5's grounding rules are unchanged by its absence |
| 2 | **Cross-encoder rerank** | Measurable retrieval quality | §8.2 **already specifies** the degradation — top-k by merged score, logged. Cutting it uses a path that has to exist anyway |
| 3 | **Query rewriting** | Elliptical and temporal questions fail | Real quality loss, no broken promise. Narrows the claim to well-formed questions, and that gets said |
| 4 | **Studio index button** | Bootstrap becomes operator-only | The command and the query-time backstop both remain, so no course is unreachable — only less convenient |
| 5 | **Feature B down to study plan only** (no generated practice) | Half of feature B | Also removes the rubric obligation, so it cuts a control *with* the thing it guards — §1.2 rule 1 permits exactly this and forbids the reverse |
| 6 | **Feature B entirely** | A headline feature | Last resort. Feature A plus honest measurement beats two half-features with no numbers |

**Read the ladder as a statement about what this project is.** Items 1–3 are all quality; items 4–6 are all scope. Nothing in the list is a safety control, an isolation boundary, or a measurement — because every one of those is either a live guarantee or the evidence that the work can be assessed at all. The ladder sacrifices *how good it is*, never *whether its claims are true*.

---

## 7. Build risks — distinct from §15's product risks

§15 lists what could be wrong with CourseMate. These are what could go wrong *building* it.

| Risk | Signal it's happening | Response, pre-decided |
|---|---|---|
| **Tutor environment consumes week 1** | S1 not done by end of day 1 | Escalate same day. Consider a hosted/managed instance; do not spend day 2 on it silently |
| **Streaming needs a rewrite in week 3** | S2 skipped or deferred | Do not skip S2. It is half a day now against three days later |
| **Retrieval quality rabbit-holes** | Two consecutive days of "tuning" | Timebox to one day; take the number and report it. §11 exists to make a mediocre number publishable rather than embarrassing |
| **Exam prep extraction eats week 3** | OCR/parsing consumes day 13 | Ladder item 5. The pre-loaded pack can be pre-cleaned — §7.2 already scopes the MVP to one loaded pack, and cleaning it by hand is legitimate |
| **Eval slips to "if there's time"** | Week 4 starts with week 3 work | This is the failure that makes the whole project unassessable (§1.3). Protect week 4 with the ladder, never by deferring measurement |
| **Design/doc drift** | A claim outlives what supports it | §17's maintenance rule. Budget the last hour of week 4 for the search-every-document pass — v7 exists because that pass caught three |

**The single most likely failure mode is the last two combined:** week 3 work bleeding into week 4, measurement getting compressed, and the delivery becoming a demo without numbers. A demo without numbers is exactly the outcome §1.3 was written to prevent — *"is that good?"* has no answer unless the claim existed first, and no evidence unless someone measured it.

---

## 8. Decisions needed, and by when

| # | Decision | Needed by | Default if undecided |
|---|---|---|---|
| 1 | ~~Streaming shape — proxy vs. mint-and-connect~~ | ~~Day 2~~ | **Closed** — mint-and-connect, adopted as design v8 §3.4 r3. S2 now only proves the ingress routing |
| 2 | **Vector store** — one engine for vectors + metadata, or two | Day 2 (ADR-001) | Whichever filters before ranking most cleanly (§6.3) |
| 3 | **Target Open edX release** — Ulmo or Verawood | Day 1 | Ulmo — current stable, and what the verification results should be recorded against |
| 4 | **Which course the demo runs on** | End of week 1 | Blocks nothing early, blocks *everything* in week 4. It needs real content and a real exam-prep pack, and per §7.7 an OCW course is demo-legal but never commercial |
| 5 | Contract version lock vs. negotiate | Week 2 | Hard lock asserted at startup — correct for a single instance (§3.5) |

Decision 4 is the one that looks administrative and isn't. Every quality number in week 4 is a number *about a specific course*, and picking it late means measuring content nobody chose.
