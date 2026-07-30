# Architecture Review — Rounds 2–4: Broken Flows, Verified and Fixed

*A self-review of the CourseMate design conducted as an architecture audit rather than an authoring pass, with each finding verified against Open edX source, documentation, or vendor docs before being written into the design. This document exists so a reviewer can see what was found, what the evidence was, and what changed — including the three places where the platform gave us the fix, and the two places where a fix of ours introduced a fresh defect.*

**Round 2 (→ design v4):** 5 broken flows (P0), 6 contradictions (P1), 9 gaps (P2).
**Round 3 (→ design v5):** a consistency audit of those fixes — 2 new defects created by them, 1 set of guarantees quietly cancelled through the scope table, 4 decisions left unstated.
**Round 4 (→ design v5 final):** an audit of round 3, which had made the build *bigger* while arguing for scope discipline. The instructor loop was cut entirely.

All fixed in `CourseMate_Complete_Design.md` except where marked *deferred by design* — and every deferral that narrows a stated guarantee now says so at the guarantee, not only in the table.

---

## What the research changed

Three findings were **stronger** than the initial review suspected, and three fixes turned out to have a **platform precedent** we could adopt rather than invent:

| Verified | Effect on the design |
|---|---|
| Open edX **publish is subtree semantics** — publishing a node publishes its children, and children removed in the source are removed at the destination | The "AI writes to draft, human publishes" gate was **not a gate**. Forced a redesign (§9.1) |
| The platform's own `content.search` app pairs **`handlers.py` signal handlers with `tasks.py` Celery tasks**, plus a **`reindex_studio`** bootstrap command with `--incremental` | Our two biggest ingestion fixes are **the platform's own pattern**, not our invention |
| **No unpublish event exists** in `openedx-events` | Confirms an unclosable gap; changed the fix from "handle the event" to "reconciliation sweep with a stated window" |
| **`COURSE_IMPORT_COMPLETED` / `COURSE_RERUN_COMPLETED` / `COURSE_UNENROLLMENT_COMPLETED` do exist** | Turned three "unhandled" items into clean subscriptions |
| Blocking **XBlock handlers are a documented cause of gunicorn `WORKER TIMEOUT`** | Upgraded the topology concern from theoretical to evidenced |
| **LiteLLM Router ships `fallbacks`, `content_policy_fallbacks`, `RetryPolicy`, `allowed_fails` cooldowns** | The fallback chain is **config, not code** — affordable inside the delivery window |
| **MIT OCW is CC BY-NC-SA** (Non-Commercial + Share-Alike) | New IP constraint the design had not stated at all |

---

# P0 — Broken flows

## 1. No path to index a course that already exists

**What broke.** Ingestion was triggered *only* by `XBLOCK_PUBLISHED`. Install CourseMate on a running Open edX, open a course published months ago, and the tutor answers *"not covered in this course"* to everything — no event ever fired for that content, and nobody re-publishes an old course to wake up a plugin. The confidence guard makes this **look like correct behaviour**, which is worse than an error. This is the most likely way a live demo fails.

**Evidence.** The platform hit the identical problem indexing Studio content for Meilisearch and solved it with a one-time command: `reindex_studio` (with `--incremental`), after which "it should not be necessary to run it again; from that point forward the indexes will be updated automatically."

**Fix (§5.1).** A `coursemate_reindex` management command mirroring that shape — `--course` / `--all` / `--incremental`, idempotent, progress-reporting, and reconciling its final count against the course tree so a partial run is visible. Runs on enable. The event pipeline becomes the *incremental* path on top of a real initial state.

---

## 2. `delete-then-insert` could permanently delete a lesson from the index

**What broke.** The v3 rule read: *delete all prior chunks for this `usage_key`, then extract → chunk → embed → write.* Embedding is a network call to a third party. A timeout, a rate-limit, or a worker crash between the delete and the write leaves the old chunks gone and nothing in their place. A published lesson students can see becomes **permanently invisible to the tutor** — reported as "not covered," so the data loss disguises itself as the abstention feature working correctly. Even the happy path has an unsearchable window.

**Fix (§5.3).** Write-then-swap:

```
1. WRITE  new chunks under usage_key@new_version   (old untouched)
2. VERIFY expected count present and readable
3. SWAP   flip the active-version pointer          ← atomic
4. GC     delete superseded versions               (background, retry-safe)
```

Retrieval filters on the active version, so stale content still can never coexist with current content — which was the original rule's actual goal. The difference: **failure now leaves the previous good state intact.** For full rebuilds the same principle applies at index scale (build into a temporary index and swap atomically — Meilisearch supports index swapping for exactly this zero-downtime case).

---

## 3. The publish event receiver ran inside Studio's request

**What broke.** `openedx-events` signals are Django signals (`OpenEdxPublicSignal` subclasses Django's `Signal`), so a `@receiver` runs **synchronously, in-process, inside the publishing request** — and `XBLOCK_PUBLISHED` is `org.openedx.content_authoring.*`, so that request is in **Studio**. Running extract → chunk → **embed** inline hangs the instructor's *Publish* button on our vendor's network I/O: 40 leaves = 40 embedding round-trips inside the request. If the embedding provider is slow, Publish is slow. If it's down, **Publish fails.**

That makes a core platform action depend on a third party we chose. No retry, no dead-letter queue, and no reconciliation existed either, so any transient failure desynchronized the index silently and permanently.

**Evidence.** The platform's own content indexer does exactly what we should: `content.search` has `handlers.py` (signal handlers) *and* `tasks.py` — "asynchronous celery task for content indexing." Separately, XBlock handlers that take too long are a documented cause of gunicorn `WORKER TIMEOUT` in Open edX.

**Fix (§5.2).** The receiver validates and **enqueues a Celery task**, then returns. All work happens in the worker, with exponential-backoff retries on transient errors, a dead-letter queue for permanent ones, and a `failed_ingestions` record surfaced by the reconciliation sweep — so a gap is **detectable**, never silent. This became **Principle 8: never degrade the platform, and never fail silently.**

---

## 4. "AI writes to draft, a human publishes" was not an approval gate

**The deepest finding.** Principle 2 — the design's central trust claim — was enforced by hoping the instructor noticed a new block.

**Evidence.** Open edX publish is **subtree semantics**: publishing a node publishes its children to the destination, and previously-published children that no longer exist in the source are removed. So if the AI parks a proposed block in a unit's draft and the instructor later fixes an unrelated typo in that unit and clicks Publish, **the AI's content goes live too** — an invisible side effect of an unrelated action, never reviewed, with no reason for the instructor to suspect it exists.

**Fix (§9.1).** Invert the flow so **approval causes the write** instead of filtering it afterward. AI proposals live in a **proposal queue in CourseMate storage, outside the course tree**, where no publish action can reach them. Accepting a proposal writes it to draft **and publishes it atomically**; rejecting archives it with a reason; editing writes the instructor's version. Because a proposal never touches the course tree until accepted, **there is no state in which unreviewed AI content can be published by accident.** Principle 2 is now enforced by storage location rather than attentiveness.

*(The modulestore publish API accepts a blacklist, so a narrower fix may exist — we don't rely on it, because the proposal queue is correct regardless of Studio UI behaviour.)*

---

## 5. Nothing said which process the reasoning layer ran in

**What broke.** The design put the tutor in an XBlock (LMS process) and described the data boundary as "in-process." Read literally, that puts rewrite → retrieve → rerank → generate — 5–20 seconds of blocking I/O — inside an **LMS web worker**. The LMS worker pool is shared with courseware rendering, so a class of 200 using the tutor during a lecture exhausts it and **takes the LMS down for students who never opened the tutor**: an availability incident caused by an optional feature.

**Evidence.** Long-running XBlock handlers causing gunicorn worker timeouts is a documented Open edX failure mode; offloading blocking work to Celery/external workers is the standard remedy.

**Fix (§3.4).** An explicit topology with three rules:

1. **Content reading must be in-platform** — `modulestore()` is a Python API, so the ingest worker is a Celery worker inside the Open edX deployment.
2. **Reasoning must be out-of-platform** — its own container, scaled and restarted without touching the LMS.
3. **The XBlock is a proxy, not an application** — authenticate, rate-limit, forward, with a hard timeout and circuit breaker so a CourseMate outage renders "tutor unavailable" instead of occupying a worker until gunicorn kills it.

---

# P1 — Contradictions

## 6. The MCP-deferral argument collapsed once topology was fixed

The v3 promotion trigger included *"when the reasoning layer needs to run out-of-process."* Finding 5 shows it must, on day one — so by the document's own criterion the trigger had already fired.

**Resolution (§6.5).** There are **two** seams, and v3 conflated them:

| Seam | Nature | MVP |
|---|---|---|
| XBlock → CourseMate service | **Network boundary, day one, non-negotiable** | Authenticated HTTPS/SSE |
| Agents → Knowledge layer | In-process, *inside* the CourseMate service | `CourseIntelligence` interface |

"In-process" never meant "inside the LMS." The out-of-process requirement is satisfied by the first seam; only the **agent→data protocol** remains deferred. The security argument for the inner boundary (four checks at one chokepoint that a new agent node cannot forget) is unchanged and still holds at one consumer.

## 7. Multi-tenant SaaS and in-process reads were both asserted

The architecture doc showed a central *"AI Platform"* serving University A, B and Company C; the design doc read content in-process from the LMS. Each university runs its own deployment — you cannot read another organisation's modulestore across the internet.

**Resolution (§3.5).** **The MVP is a per-instance plugin: one deployment, one tenant.** `tenant` stays in the schema and cache keys (retrofitting an isolation key later is expensive) holding a single constant. Multi-tenant SaaS is deferred *for reasons bigger than effort* — different content-access path, cross-instance auth, data residency, per-tenant key management. Per-**student** isolation is **not** deferred; it is the boundary that matters in the MVP.

## 8. The instructor Exam Prep Pack had no defined home — and an IP problem

v3 said an instructor's pack "becomes course content" while the architecture doc said uploads live in our storage, *not* Open edX. Both were asserted.

**Resolution (§7.7).** **All exam-prep material lives in CourseMate object storage keyed by `offering_id`; none is written to the Contentstore.** Two reasons — and the first is a leak we would otherwise have built:

1. **Course exports would carry the exam bank.** Contentstore content is included in OLX exports, and exports are routinely shared between institutions. Storing past papers there builds a mechanism that leaks one university's exam bank to another.
2. The course data model can't hold per-question structured records.

**Principle 1 still holds:** we are the storage; **Open edX remains the permissions authority** — access is decided by asking the platform who is enrolled in that offering with what role.

**New IP constraint the design never stated.** Verified: MIT OCW is **CC BY-NC-SA**.
- **Non-Commercial** → OCW is **development and demo only**; never ingested into a paying institution's namespace, never shipped if CourseMate is sold.
- **Share-Alike** → derivatives must carry the same licence, which does not compose with Apache-2.0; so OCW stays *data indexed at demo time*, never redistributed in the repo.
- **Attribution** → credited to MIT and the author.

Institutional past papers are institutional IP: they stay in their offering's namespace, never cross tenants, never enter an export, and are deleted with the offering.

## 9. Every human-in-the-loop step was a UI that didn't exist

Four instructor interactions were assumed and none designed, with **no notification mechanism** — so the safety loop was drawn closed but had no arrow reaching a human. There was also no surface for an instructor to *upload* a pack, since the tutor is a student-facing block.

**Resolution (§9.2).** All four surfaces named, placed in the **Studio view of our own XBlock** (the cheapest surface inside a tool instructors already use; needs no Frontend Plugin Slot), each with an explicit built/deferred status. Notification: **badge count + digest email** to course staff. Stated plainly — if notification is cut, the loop's *latency* degrades but its *safety* does not, because nothing publishes without an accept.

## 10. `XBlockAside` attaches per block, not per lesson

"Auto-appears on every lesson" was wrong. An aside renders alongside **every block it applies to** — a unit with a video, four HTML blocks and three problems would render **eight tutor instances on one page**: eight chat UIs, eight `user_state` records, eight potential LLM calls. The flagship stage shipped a visibly broken page.

**Evidence.** The XBlock Runtime API provides exactly the needed filters: `XBlockAside.should_apply_to_block()` and the runtime's `get_applicable_aside_types()`.

**Fix (§3.1).** The aside applies **only at `vertical` (unit) level** — one tutor per unit.

## 11. The confidence pipeline couldn't stream, and had no latency budget

Gate 3 checked claims **after** generation, requiring the full answer before showing anything — ruling out token streaming. With rewrite → retrieve → rerank → generate → verify, that's 3–4 serial model calls and no stated p95. Students compare this to ChatGPT; a 12-second blank box reads as broken.

**Evidence.** The 2026 pattern is to **stream the response and run verification in parallel**, surfacing a banner within ~500 ms of completion; cheap string/semantic assertion-matching against retrieved chunks catches a meaningful share of unsupported claims at near-zero latency.

**Fix (§8.2, §8.5).** A published latency budget (**p95 < 2 s to first token**, itemized by stage), and verification restructured: gate 1 runs *before* generation so nothing streams that failed the retrieval bar; gates run **in parallel** with streaming; cheap assertion-matching first, model-based checks only when inconclusive; failures raise a visible inline flag rather than silently rewriting text the student already read. The **reranker also got a home** — a CPU cross-encoder inside the CourseMate service (~100–300 ms for 20 pairs), with a stated skip-under-load degradation.

---

# P2 — Gaps closed

| # | Gap | Fix |
|---|---|---|
| 12 | **No cost model** | §12: ingestion ≈ cents/course one-time; **~$0.02/question, ~$0.40/student/term, ~$80/course-of-200/term**; the ~15× multi-agent multiplier priced (~$1,200/course) as the concrete reason the graph stays shallow; exam-week spike bounded by cache + budgets |
| 13 | **Struggle signals de-anonymize in small cohorts** | §10.3: **k-anonymity floor (k=5)** — suppressed entirely below it, not rounded. §9.3: signal derived from Completion/Grades + **server-side** aggregation, not student-writable `user_state_summary` |
| 14 | **Cache key allowed a staff→student leak** | §6.4: key now includes a hash of the **effective permission scope and applied filters**; personal-namespace results never cached |
| 15 | **No cascade from platform lifecycle** | §10.7: consume **`COURSE_UNENROLLMENT_COMPLETED`** (verified to exist); course deletion drops the namespace; **user retirement** — verified to be a configurable pipeline of APIs explicitly designed to call **external services holding PII**, so we expose an idempotent deletion endpoint. *No retirement event exists*, so this is an API integration, not a receiver |
| 16 | **Feature B had no evaluation at all** | §11.3: a 30-item rubric on generated practice — **validity, CLO alignment, provenance, difficulty calibration**. A wrong generated question is worse than a wrong answer: students study from it |
| 17 | **τ can't be calibrated at pilot scale** | §8.5: ~15 negatives cannot calibrate a threshold. Restated as **initialized** from the pilot and **refined** from logged production abstentions, reported with a confidence interval |
| 18 | **Reranker was unlisted infrastructure** | §8.2: named component, CPU cross-encoder in the CourseMate service, budgeted at 250 ms, with a degradation mode |
| 19 | **Unpublish unhandled** | §5.4: **verified — no unpublish event exists.** Mitigated by a nightly **reconciliation sweep** plus an on-publish sweep, **with the residual window stated rather than claimed closed** |
| 20 | **Course import/rerun behaviour unknown** | §5.4: **`COURSE_IMPORT_COMPLETED` and `COURSE_RERUN_COMPLETED` exist** — subscribe and run **one budgeted bulk index** instead of relying on a flood of per-block events, with a per-course cost ceiling that pauses and alerts |

---

# The managerial finding: scope vs. timeline

The design describes a **product**; the plan describes **3.5 weeks, one engineer**. Round 1's improvements (security, caches, rewriting, human eval, question schema) made the gap wider, and round 2 added more (bootstrap, proposal queue, reconciliation, topology, instructor surfaces).

Handling this with a closing caveat reads as scope not confronted. **§1.2 now puts a Built-vs-Designed table on page one** — twelve rows, each naming what ships in week 8 and what is designed but deliberately not built, with the note that every deferred item has its schema, seam, or interface already in place so it is an *addition*, not a rewrite.

Two features in 3.5 weeks was never the risk. **Two features plus twenty supporting subsystems** was. Saying which twenty are not being built — before anyone asks — is what makes it a plan.

---

---

# Round 3 — consistency audit of the round-2 fixes (v5)

A third pass reviewed v4 *as a whole* rather than fix-by-fix. It found that **two of the round-2 fixes created new problems**, one cancelled guarantees through the cut line, and four left decisions unstated. Recorded here because a fix that introduces a fresh defect is exactly what a reviewer looks for.

## The two fixes that broke something else

**R3-1. Accept-and-publish re-opened the subtree bug, pointing the other way.** The proposal queue (finding 4) correctly stopped AI content from being published by accident. But its accept action — *"write to draft AND publish, atomically"* — ignored that subtree semantics are still true at the moment of acceptance. If the instructor had **their own unpublished work-in-progress** in that unit, accepting an unrelated AI suggestion would publish their unfinished edits too. Closed AI→student; opened instructor→student.
**Fixed (§9.1):** accept checks the target container for other pending draft changes first, and if any exist, shows exactly what else would go live before offering *publish-only-this* (scoped publish via the modulestore blacklist — flagged unverified), *publish-everything-having-seen-the-list*, or *cancel*. New invariant: **no publish caused by CourseMate ever makes content live that the instructor has not seen in that moment** — which covers both directions.

**R3-2. Routing personal practice through instructor approval made Feature B undemonstrable.** v4 sent every generated item to the proposal queue, while the review UI was scoped as a stub — so no practice question could ever reach a student. The deeper error was conceptual: a practice question generated for the one student who asked is a **personal study aid**, not course content, and gating it behind a human is unworkable *and* protects nobody.
**Fixed (§9.0):** an explicit line — *does anyone other than the asker see it?* Personal output is governed by Principle 4 (grounded, cited, abstains, and **measured**); course content is governed by Principle 2 (queue + accept). Students can promote an item across the line. Consequence acknowledged: for the personal path **measurement is the control**, which is why the Feature B rubric moved into Built.

## The cut line was cancelling guarantees

**R3-3.** §1.2 deferred the **reconciliation sweep** — the *only* mitigation for unpublished content — which would have made the Principle 3 exposure unbounded rather than "one sweep interval." It also deferred the **Feature B rubric** while §11.3 argued that feature is the more dangerous one, and listed **k-anonymity** as "tuning" when it is a privacy control. Principle 5 asserted human raters that the MVP would not have.
**Fixed (§1.2):** all three moved into Built, and the table now carries two governing rules — *nothing carrying a stated guarantee may be deferred silently*, and *where a deferral narrows a claim, the claim says so*. Principle 5 now scopes itself honestly.

## Decisions v4 left unstated

| | Left open | Decided (v5) |
|---|---|---|
| **R3-4** | Where chat history lives, once reasoning moved out of the LMS | Platform-owned in `Scope.user_state`; the XBlock sends a rolling window; **the service is stateless**. The alternative (service-side conversation store) was rejected: it would falsify the privacy claim and duplicate PII outside the reach of platform retirement (§3.1) |
| **R3-5** | How the XBlock→service hop is authenticated — the one hop Open edX doesn't secure for us | Short-lived **signed JWT** per request, service not internet-exposed (private network/mTLS), and **authorization re-derived server-side** so a forged enrollment claim buys nothing. Separate credential for ingest/invalidation APIs (§3.4) |
| **R3-6** | What actually triggers bootstrap — "runs automatically when enabled" had no event to hang on | Three entry points: a **button in the Studio view**, the management command, and **empty-index detection at query time** that says *"this course is still being prepared"* and enqueues the job — so the demo-killer degrades to *informative* rather than *broken* (§5.1) |
| **R3-7** | Where the fallback model runs, and what it costs — the reranker got a home in v4 and this didn't | Rewrite runs on CPU within its 300 ms budget; **fallback generation on CPU will not meet 800 ms TTFT**, so degraded mode is slower *and says so in the UI*. A slow honest answer beats an outage; a silent one is what's forbidden (§8.2) |

## Also corrected

- **Receivers existed only in the CMS**, but `COURSE_UNENROLLMENT_COMPLETED` is a `learning` event firing in the **LMS** — and both the permission cache and personal-data scoping depend on it. Receivers now in both processes, with an invalidation notice to the service (§3.4 rule 4).
- **Tool signatures took `course_id`** while §7.4 insists the isolation unit is `offering_id` — a scoping bug that would only surface once the same course ran twice. All four tools now key on `offering_id` (§6.5).
- **Cost had no sensitivity analysis.** "20 questions per student per term" is the assumption a reviewer pushes on. Added: 50 → $200/course, 100 → $400/course, with embedding and model rates named so the arithmetic is re-runnable (§12).
- **The summary claimed "nothing the AI produces reaches a student without a human accept"** — which describes Feature A being switched off. Rewritten to say *course content*. Also said content is read "in-process," the exact word §3.4 exists to disambiguate; now "in-platform."
- **The architecture doc still called `user_state_summary` "the anonymous who's-stuck signal, by construction"** in §4.2, contradicting its own corrections elsewhere. Now carries the correction inline, along with the per-block (not per-lesson) aside precision.
- **Latency budget** omitted the service hop it had just introduced (now 30 ms; total ~1.6 s).

---

# Round 4 — auditing the audit (v5 final)

Round 3's fixes were correct and made the build **bigger**. Round 4 caught that, plus five smaller consequences. Recorded because the pattern itself is the lesson: *each round fixed real defects and introduced a smaller one.*

**R4-1 — the scope section grew while arguing for scope discipline.** §1.2 exists to argue that naming what you're *not* building is what makes a plan. Round 3 moved four items **into** Built (reconciliation sweep, Feature B rubric, k=5 floor, minimal review UI), added a row, and removed nothing. Each move was individually justified — they were cancelling stated guarantees — but "correctness forced it" is not a schedule.
**Fixed:** the **entire instructor loop is deferred** — struggle signals, aggregation, proposal generation, review UI, notifications. It is four subsystems supporting neither headline feature, and §9.3 already conceded its signal is biased in the MVP. The consequence is stated as a *strengthening*: **the MVP generates no course content at all**, so Principle 2 is satisfied by construction rather than by a review screen. The queue schema and accept/conflict logic stay fully specified but dormant. A new rule now governs the table: *a control may only be deferred together with the feature it guards* — which is why k=5 could leave with struggle signals, and why the reconciliation sweep could not leave at all.

**R4-2 — three self-hosted inference services had crept in.** Round 2 gave the reranker a home; round 3 then quietly homed a self-hosted rewrite model and a self-hosted fallback `llama3` without costing either.
**Fixed:** **one** self-hosted service (the CPU reranker). Rewrite moves to a cheap hosted model (~$0.0002/query — still ~0 in §12) for zero infrastructure; the local fallback is deferred, since the availability argument was never "the model is local" but "survive one vendor's outage," which a second provider does in config. Stated cost of the deferral: a simultaneous outage of both providers means the tutor is unavailable, honestly (risk 29).

**R4-3 — the Feature B rubric shipped with its raters deferred.** Its four dimensions are all human judgements; §11.2(b) had deferred human rating wholesale. A stated control with nobody performing it.
**Fixed:** split the tiers — **single-rater assessment ships** with *single rater* named as the limitation; the **blind two-rater study** with inter-rater agreement is what's deferred.

**R4-4 — "promote to course" existed only in a table and a flow arrow** — no schema field, no surface, no scope line: the same "assumed a UI" error this review found in round 2. **Fixed:** an `origin` field (`ai_proposal | student_request`) in the queue schema, with the path itself deferred alongside the loop.

**R4-5 — the query-time bootstrap backstop was a student-triggerable expensive job.** Refreshing the page would queue repeated full-course re-indexes. **Fixed:** in-flight lock, cooldown, and the §10.8 per-course cost ceiling applied to that path.

**R4-6 — stale text in the sibling docs.** The architecture doc still carried v4's buggy `ACCEPT writes to draft AND publishes, atomically`, two lines above its own explanation of why that's unsafe; the plain-language plan still promised "practice maker (with teacher approval)," contradicting the personal/course split; §5.6 presented self-serve upload as operational. All corrected.

**Where this stops.** Four rounds in, the findings have moved from *broken flows* → *contradictions* → *consequences of fixes* → *scope drift*. That's diminishing returns on design review and rising returns on building: the remaining open items (§3.6) are all things a running Tutor instance answers in an afternoon and no amount of reading answers at all.

---

# Sources

- [Split: the versioning, structure saving DAO — openedx/edx-platform Wiki](https://github.com/openedx/edx-platform/wiki/Split:-the-versioning,-structure-saving-DAO) — publish subtree semantics, blacklist, child removal
- [Developing Course Units — Building and Running an Open edX Course](https://edx.readthedocs.io/projects/open-edx-building-and-running-a-course/en/open-release-palm.master/developing_course/course_units.html) — draft/published behaviour in Studio
- [Open edX Events — Events reference](https://docs.openedx.org/projects/openedx-events/en/latest/reference/events.html) — full event inventory; `COURSE_IMPORT_COMPLETED`, `COURSE_RERUN_COMPLETED`, `COURSE_UNENROLLMENT_COMPLETED`; no unpublish or retirement event
- [Open edX Events — Event Bus concepts](https://docs.openedx.org/projects/openedx-events/en/latest/concepts/event-bus.html) and [External event bus and Django Signal events](https://docs.openedx.org/projects/openedx-events/en/latest/decisions/0004-external-event-bus-and-django-signal-events.html) — `OpenEdxPublicSignal` subclasses Django `Signal`
- [openedx.core.djangoapps.content.search package](https://docs.openedx.org/projects/edx-platform/en/release-teak/references/docstrings/openedx/openedx.core.djangoapps.content.search.html) — `handlers.py` + `tasks.py` Celery indexing, `rebuild_index`/`reset_index`/`upsert_*`
- [Index Studio content using Meilisearch (PR #34310)](https://github.com/openedx/openedx-platform/pull/34310) and [Sumac dev/operator release notes](https://docs.openedx.org/en/latest/community/release_notes/sumac/dev_op_release_notes.html) — `reindex_studio`, `--incremental`, one-time then automatic
- [Zero downtime index deployment — Meilisearch](https://www.meilisearch.com/blog/zero-downtime-index-deployment) — atomic index swap
- [Runtime API — XBlock API Guide](https://docs.openedx.org/projects/xblock/en/latest/runtime.html) — `should_apply_to_block()`, `get_applicable_aside_types()`
- [Open edX Troubleshooting Guide](https://blog.lawrencemcdaniel.com/open-edx-trouble-shooting-guide/) and [Celery Workers Configuration](https://openedx.atlassian.net/wiki/spaces/SUST/pages/1317011477/Celery+Workers+Configuration) — XBlock handlers and gunicorn worker timeouts
- [Enabling the User Retirement Feature](https://docs.openedx.org/projects/edx-platform/en/latest/references/docs/scripts/user_retirement/docs/index.html) and [Implementation Overview](https://docs.openedx.org/projects/edx-platform/en/latest/references/docs/scripts/user_retirement/docs/implementation_overview.html) — pipeline of building-block APIs, third-party PII deletion
- [Fallbacks (Provider Failover) — LiteLLM](https://docs.litellm.ai/docs/proxy/reliability) and [Router — Load Balancing](https://docs.litellm.ai/docs/routing) — `fallbacks`, `content_policy_fallbacks`, `context_window_fallbacks`, `RetryPolicy`, `allowed_fails` cooldowns
- [What are the requirements of use for MIT OpenCourseWare?](https://mitocw.zendesk.com/hc/en-us/articles/4414774353051-What-are-the-requirements-of-use-for-MIT-OpenCourseWare) and [MIT OpenCourseWare — Creative Commons wiki](https://wiki.creativecommons.org/wiki/MIT_Open_CourseWare) — CC BY-NC-SA terms
- [Stream RAG (arXiv 2510.02044)](https://arxiv.org/abs/2510.02044) and [Fast and Faithful: Real-Time Verification for Long-Document RAG](https://arxiv.org/html/2603.23508) — streaming with parallel verification, low-latency assertion checking
