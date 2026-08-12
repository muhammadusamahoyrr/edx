# CourseMate — Complete System Design

*An AI layer on Open edX delivering two features: an AI Course Tutor and a Final Exam Prep mode. This is the single source of truth — Open edX integration facts, the AI architecture, the deployment topology, and the security model.*

*Grounding: platform integration points were read from real source (`XBlock`, `openedx-events`, `xmodule/modulestore`, `openedx/core/djangoapps/content/search/`) and verified against Open edX documentation; each is marked **verified** with its source. AI-side choices are grounded in current 2025–2026 practice. Anything unproven on a running instance is flagged, not hidden.*

**Version history**
- **v8 (this version)** — **one architectural change, found while sequencing the build rather than by review.** The XBlock stops being a proxy: it **mints a short-lived JWT and the browser streams from the CourseMate service directly**, on a same-origin path routed at the ingress (§3.4 rule 3). The proxy shape re-created the very incident §3.4 exists to prevent — a streaming relay holds a gunicorn worker for the whole answer, and the worker pool is exhausted by *occupancy*, not by computation, so 200 concurrent streams is 200 occupied workers whether or not any of them compute anything. Rate limiting, timeout and circuit breaker move to the service, where §10.8 already wanted them; chat history stays platform-owned but is now carried by the browser and written back through a handler (§3.1); the JWT expiry widens from seconds to minutes; §4, §6.5, §8.2, §12, §13.1 and §14 follow. **Nothing about ingestion, retrieval, grounding, or the human loop changed** — this is the request path only.
- **v7** — **source re-verification against current Open edX `master` and official docs (2026-07-30).** Thirteen platform claims held exactly, including the whole event inventory, the one-event-per-parent publish behaviour, the absence of an unpublish event, the Meilisearch index config, and the LiteLLM Router feature set. Three corrections are applied here: **§10.4** dropped a course-export exclusion mechanism that does not exist and replaced it with a stronger by-construction guarantee (the thin XBlock holds no credentials at all), with the same stale claim removed from §3.1 and the §1.2 scope table; **§5.4** now lists `XBLOCK_CREATED`/`XBLOCK_UPDATED` as deliberately refused, because they fire on draft edits and their unexplained absence was an invitation to a Principle 3 violation; **§3.6** closes the Celery branch-setting question from source — it is thread-local and inherits nothing, so pinning `published_only` is settled, and the adapter must own the context rather than expose it. Two further findings are recorded in `CourseMate_Repository_Structure.md` rather than here because they affect the build, not the design: `reindex_studio` has dropped `--incremental` and become a resumable task-enqueuer (which is the better pattern for `coursemate_reindex`, §5.1), and no target Open edX release is pinned anywhere. No architectural decision changed.
- **v2** — consolidated design; solved the exam-prep data-sourcing problem.
- **v3** — design review round 1: MCP reframed as a boundary, Learning Core schedule claims removed, confidence-aware grounding, query rewriting, metadata extensions, question schema, human evaluation, fallback chain, security section, cache tiers.
- **v4** — architecture review round 2, after research verification. Five broken flows fixed: **bootstrap ingestion** (§5.1), **write-then-swap** (§5.3), **asynchronous ingestion** (§5.2), **the proposal queue replacing draft-writes** (§9.1), and **explicit deployment topology** (§3.4). Two contradictions resolved: **tenancy model** (§3.5) and **which boundary is in-process** (§6.5). Added §9 (human-in-the-loop surfaces), §12 (cost & capacity), and a **Built vs. Designed** cut line (§1.2).
- **v6** — folds back what was settled while writing the stakeholder proposal. Adds a **delivery plan and a definition of done committed in advance** (§1.3); names the **five layers explicitly**, with the two facts a stack diagram hides — Integration is the only layer inside the customer's platform, and Evaluation runs offline rather than in the request path (§4); marks the proposal queue as *dormant* in the architecture diagram so the MVP's actual behaviour is readable from the picture. No design decisions changed — this is consolidation, and the companion documents are indexed at the end.
- **v5** — two consistency audits of v4 and of v5's own first pass. **The scope table shrank:** an earlier v5 draft let correctness fixes inflate the Built column without removing anything, which is how an honest scope argument becomes a dishonest one — so **the entire instructor loop is now deferred** (§1.2, §9.2, §9.3), the MVP generates **no course content at all**, and self-hosted inference is down from three services to one (§8.2). Also: Feature B's rubric now has a rater (§11.2), the query-time bootstrap backstop is rate-guarded (§5.1), and the promote-to-course path has a schema field instead of only a diagram (§9.0). Earlier in v5: **Accept now handles the mirror-image subtree bug** — accepting a proposal must not publish the instructor's own unfinished drafts (§9.1). **Personal output is separated from course content** (§9.0), because routing a student's own practice question through instructor approval made Feature B undemonstrable and confused two different risks. **Receivers exist in both LMS and CMS** (§3.4), since the enrollment events we depend on are `learning` events that fire in the LMS. **The cut line may no longer cancel a guarantee silently** (§1.2) — the reconciliation sweep, Feature B rubric and k-anonymity floor moved into Built. Specifies four things v4 left open: **where chat history lives** (§3.1), **how the XBlock→service hop is authenticated** (§3.4), **what triggers bootstrap** (§5.1), and **where the fallback model runs and what it costs in latency** (§8.2). Plus cost sensitivity (§12) and `offering_id` consistency across the tool signatures (§6.5).

---

## 0. Principles

**Platform principles (how we sit on Open edX):**
1. **Open edX is the source of truth** for courses, permissions, and progress. We only ever *extend* it through sanctioned extension points; we never modify or fork the core.
2. **The AI proposes; a human approves — for anything that enters the course.** No AI-generated content becomes part of a course, or reaches students other than the one who asked, without an explicit human accept action (§9.1). *(v4: the mechanism changed — see §9.1; the old "write to draft and let the instructor publish" did not actually enforce this. **v5 scopes the principle:** personal, ephemeral output — a tutor answer, or a practice question generated for the student who requested it — is governed by the grounding rules of Principle 4, not by instructor approval. Gating one student's private study aid behind a human would be unworkable *and* pointless, since it reaches nobody else. The distinction is drawn precisely in §9.1.)*
3. **The AI only learns from published, authorized content.** Ingestion is scoped to the published branch, filtered by enrollment, and isolates personal uploads per student.

**AI principles (how the intelligence behaves):**
4. **Grounded, cited, and confidence-aware.** Every answer is built from retrieved content and cites its source block. Below a calibrated confidence threshold, the tutor **abstains**. Stated as a measurable rule, not as "the tutor never fills gaps" — which is unprovable.
5. **Measured, not assumed.** Quality is scored by an automated harness on every run, and by **human raters at milestones**, reported with sample size and limitations named. *(Honest scoping: the two-rater human study falls outside the delivery window (§1.2). Within the MVP this principle is met by the automated harness plus the abstention audit — stated here rather than left to imply a human study that hasn't happened.)*
6. **Cheap by default, strong when needed.** A small cheap model handles routine steps (classification, query rewriting, simple lookups); a strong model is used only where reasoning demands it. *Both are hosted — see §8.2 on why "cheap" does not have to mean "self-hosted," and what it would have cost to assume otherwise.*

**Engineering principles:**
7. **Boundaries are cheap; protocols are not.** Keep seams that buy security or migration safety; defer the heavyweight *implementation* of a seam until a second consumer justifies it.
8. **Never degrade the platform, and never fail silently.** *(added in v4)* CourseMate must not be able to slow down, block, or break any core Open edX action — publishing, rendering courseware, importing a course. And when our own pipeline fails, it must be **loud and detectable**, never a quiet gap that surfaces later as "the tutor doesn't know that lesson."

---

## 1. Scope

### 1.1 The two features
- **Feature A — AI Course Tutor.** Inside a lesson, a student asks a question and gets an answer grounded strictly in *their* course, cited to the source lesson, optionally taught Socratically.
- **Feature B — Final Exam Prep.** A CLO-organized study plan and practice drawn from course content **plus** the course's past papers, slides, and CLOs (§7).
- **Safety loop — human approval** for any AI content entering a course (§9).

### 1.2 What actually ships, and what is designed but not built (the cut line)

This document describes a **product**. The delivery window is **~3.5 weeks, one engineer**. Those are not the same size, and pretending otherwise is the fastest way to lose a review. So the line is drawn explicitly, up front:

| | **Built and demonstrated (Weeks 5–8)** | **Designed here, deliberately not built** |
|---|---|---|
| **Surface** | Student-facing XBlock, per-unit, one course; minimal Studio view (config + "Index this course") | XBlockAside auto-attach (§3.1) |
| **Ingestion** | Bootstrap (3 triggers) + publish-triggered incremental, async via Celery, write-then-swap, **nightly reconciliation sweep** | Import/rerun handlers (§5.4) |
| **Retrieval** | Semantic (our vector store) + metadata filters, query rewriting, cross-encoder rerank | Meilisearch lexical half of hybrid (§6.1) |
| **Reasoning** | Shallow LangGraph; **cheap hosted + strong hosted** model via the LiteLLM Router's fallback/cooldown config | **Self-hosted local model** (§8.4), multi-agent depth, Socratic tuning beyond a basic mode |
| **Data boundary** | In-process `CourseIntelligence` interface with authz + audit; signed-JWT service hop | MCP transport (§6.5) |
| **Exam prep** | One pre-loaded pack: CLO list + past papers → study plan + **personal** CLO-tagged practice | Self-serve upload UIs (instructor *and* student), OCR for handwriting, difficulty calibration |
| **Course-content generation** | **Nothing.** The AI proposes no course content in the MVP — so nothing needs approval (see below) | The whole instructor loop: struggle signals + k=5 floor, proposal generation, review UI, notifications, accept/conflict flow (§9) |
| **Tenancy** | Single instance, single tenant | Multi-tenant SaaS (§3.5) |
| **Evaluation** | Ragas pilot (20–30 q), abstention audit, Feature B rubric — **single rater, limitation named** | **Blind two-rater study** (§11.2b) |
| **Security** | Authz + audit at the boundary, no credentials in the block at all (§10.4), per-student isolation, encryption, deletion API | Retirement-pipeline registration (§10.7) |

**The biggest cut is row 7, and it deserves its reasoning in full.** Earlier versions of this table kept the instructor loop in Built because Principle 2 depends on it. That was backwards. The loop is **four subsystems** — aggregation, proposal generation, a review UI, notifications — supporting *neither* headline feature, resting on a signal §9.3 admits is biased in the MVP, and its absence is invisible in a demo. So:

> **In the MVP the AI generates no course content at all.** It answers questions and produces personal study material (§9.0). Nothing it produces is destined for a course, therefore nothing requires instructor approval.

Principle 2 is then satisfied **by construction rather than by a UI** — which is a stronger position, not a weaker one. The proposal queue's schema and its accept/conflict logic stay fully specified (§9.1) because they are the design's answer to a real platform hazard, and because the moment content generation is switched on they are what makes it safe. They are designed and dormant, not hand-waved.

**Three rules govern this table, because a cut line can quietly cancel a promise:**

1. **A control may only be deferred together with the feature it guards.** The k=5 anonymity floor leaves Built in this version — but only because struggle signals leave with it. Deferring a control while shipping the thing it protects is what's forbidden; deferring both together is just scope.
2. **Nothing that carries a *live* guarantee may be deferred silently.** The **reconciliation sweep** stays in Built for exactly this reason: it is the only mitigation for unpublished content (§5.4), and cutting it would make the Principle 3 exposure unbounded rather than "one sweep interval." The **Feature B rubric** stays because personal practice reaches students with no human gate, so measurement *is* the control (§11.3).
3. **Where a deferral narrows a claim, the claim says so** — see Principle 5 (human study), §5.4 (import/rerun), §7.2 (upload UIs), §8.4 (local model), §11.3 (single rater).

**Why this belongs in the design document and not in a footnote:** every deferred item above is *designed* — the schema, the seam, or the interface exists, so the deferred work is an addition, not a rewrite. And this table got **smaller** in v5, not larger: an earlier revision let correctness fixes inflate Built without removing anything, which is how honest scope arguments quietly become dishonest ones.

### 1.3 Delivery, and a definition of done written in advance

| Week | Work |
|---|---|
| 1 | Project setup, component shells. **Verify the five open platform behaviours (§3.6) on a running instance** — these gate design choices, so they go first |
| 2 | Working tutor on one lesson: bootstrap index, retrieval, citation, conversation memory |
| 3 | Socratic mode, exam-prep flow, model failover, evaluation harness. Deployed |
| 4 | Documentation, demo recording, results presented |

**What "done" means, committed before building rather than argued afterwards:**

| | **Will be demonstrable** | **Will not exist** |
|---|---|---|
| Tutor | Answering real questions on a real course, citing sources, refusing what is not covered | Automatic presence on every lesson (Aside) |
| Ingestion | Bootstrap, publish-triggered updates, nightly reconciliation | Import/rerun handlers |
| Exam prep | Study plan and CLO-tagged practice from a pre-loaded pack | Self-serve upload screens |
| Quality | Measured numbers for both features, with limitations published beside them | Two-reviewer blind scoring |
| Platform impact | No core changes, no fork, no AI work inside LMS workers | Multi-tenant deployment |
| Course content | **The AI writes none** — so nothing needs approval (§9.0) | Instructor loop, review UI |

**Why commit to this in the design document.** A demo is easy to argue about afterwards — *"is that good?"* has no answer unless the claim existed first. Writing the boundary down before building makes the result assessable against a fixed statement, and it is the same discipline applied to every other decision here: state the claim, state its limits, then let it be checked.

---

## 2. The whole system in one picture

```
   PROFESSOR                                              STUDENT
       │                                                     ▲
       │ authors lesson in                                   │ asks "explain deadlock"
       ▼                                                     │ gets answer + citation
  ┌──────────┐                                        ┌──────┴──────┐
  │  STUDIO  │                                        │    TUTOR    │
  │  (draft) │                                        │  (XBlock in │
  └────┬─────┘                                        │  the lesson)│
       │ clicks PUBLISH                                └──────▲──────┘
       ▼                                                      │ retrieve (scoped, cited)
  ┌──────────┐    XBLOCK_PUBLISHED    ┌───────────┐    ┌──────┴───────────┐
  │  EVENT   │ ─────────────────────▶ │ INGESTION │──▶ │  VECTOR STORE +  │
  │   BUS    │  (container → resolve  │ (async    │    │  METADATA STORE  │
  └──────────┘   down to leaves)      │  worker)  │    │                  │
       ▲                              └───────────┘    └──────────────────┘
       │                                    ▲                    ▲
  BOOTSTRAP command ──── first-time full ───┘                    │
  (existing courses)          index                              │
                                                                 │
   INSTRUCTOR / STUDENT ── uploads CLOs, past papers, slides ─────┘
        (Exam Prep Pack, §7)         extract → CLO-tag → namespace
```

**One sentence:** a professor publishes → the platform fires an event → an async worker ingests the published lesson → a student asks inside that lesson → the tutor answers from that index and cites it.

**The bootstrap arrow is not decoration.** Without it, a course published before CourseMate was installed is invisible to the tutor forever, because no event will ever fire for it (§5.1).

---

## 3. How CourseMate attaches to Open edX

### 3.1 Where our code runs
The tutor is delivered as an **XBlock** (verified: XBlock is Open edX's Python component framework), following the `open-craft/xblock-ai-evaluation` precedent where it applies: `Scope.user_state` for private per-student chat memory, `Scope.settings` for **non-secret** instructor config, and **LiteLLM** for model calls — though for us LiteLLM runs in the service, not the block (§3.4), and the block therefore stores no credentials at all (§10.4, corrected in v7).

**Staging:** per-unit XBlock (MVP) → XBlockAside → *Frontend Plugin Slot for a floating tutor, deferred (slot system still maturing).*

**Where the conversation lives — and why the reasoning service is stateless** *(specified in v5)*. This needed pinning down once reasoning moved out of the LMS (§3.4), because the rewrite node depends on chat history (§8.1) and two designs were possible:

> **Chat history is owned by the platform in `Scope.user_state`. The CourseMate service holds no conversation state.**

The alternative — the service keeping its own conversation store — was rejected deliberately: it would make "each student's chat is kept privately by the platform" **false**, it would duplicate PII into a second system that platform user-retirement doesn't reach, and it would give a stateless service a stateful dependency for no gain. Passing history in the request payload costs a few KB and keeps the privacy claim literally true.

**The round trip, now that the browser talks to the service directly** *(v8, §3.4 rule 3)*. Ownership is unchanged; the courier is:

```
student_view   → renders the chat UI seeded with history from Scope.user_state
json_handler   → mints a JWT                                    (~ms, LMS worker freed)
browser        → streams question + last N turns → CourseMate service
browser        → posts the completed turn back to json_handler  → Scope.user_state
```

**Persisting through the platform is not a formality** — it is what keeps every claim in this section true. The service never writes the turn, so it never holds conversation state; the platform remains the only copy, so user-retirement still reaches all of it; and a student deleting their data through the platform still deletes the conversation without us doing anything.

Consequences worth stating: the payload is bounded (a rolling window, trimmed by token budget), and history is **never** written to our logs or the response cache (§6.4). One consequence is new in v8 — **a dropped connection can lose the last turn**, because the write happens after the answer completes. That is a visible, recoverable annoyance rather than a correctness problem, and the alternative (the service persisting turns itself) is exactly the design rejected above.

**Aside scoping — a correctness requirement, not a preference.** An `XBlockAside` attaches to **every block it applies to**, not to every lesson. A unit containing a video, four HTML blocks and three problems would render **eight tutor instances on one page** — eight chat UIs, eight `user_state` records, eight potential LLM calls. The platform provides exactly the filter needed (verified against the XBlock Runtime API): the aside overrides **`should_apply_to_block()`**, and the runtime additionally filters via **`get_applicable_aside_types()`**. Our aside applies **only at `vertical` (unit) level**, giving one tutor per unit. Without this the flagship stage ships a visibly broken page.

### 3.2 The three ways we read the platform
- **In-process content read (verified).** `modulestore()` → `get_course()` / `get_item(usage_key)` / `get_items()`, run inside a **`branch_setting(ModuleStoreEnum.Branch.published_only)`** context (verified: a real context manager), so draft content is structurally unreadable during ingestion.
- **Events (verified).** Receivers on the `content_authoring` events. `XBLOCK_PUBLISHED` payload `XBlockData` carries `usage_key`, `block_type`, optional `version`. **Verified firing behaviour:** publishing a parent with changes in multiple children fires **one event with the parent's details** — so ingestion must resolve the container down to leaves (§5.2).
- **REST APIs.** Enrollment / Grades / Completion for progress; Course Blocks for structure. Not relied on for lesson *text*.

### 3.3 Storage evolution and how we stay compatible
Current deployments store course content in the **Split Mongo Modulestore**. Content Libraries v2 is backed by Blockstore, and the community's stated direction is toward **Learning Core**. **We assert no timeline, sequence, or end state** — that is the community's to decide.

> **Open edX is evolving toward Learning Core. This design isolates all content access behind a thin adapter so it remains compatible with future storage changes.**

Every content read goes through one module — `content_adapter.py`, exposing `get_block(usage_key)` / `iter_leaves(usage_key)` / `get_course_meta(course_key)`. If the store changes, that module changes; nothing else does.

### 3.4 Deployment topology — which process runs what (NEW in v4, and load-bearing)

The previous version never said where the reasoning layer runs. Read one way, it put LLM calls inside an LMS web worker. That reading is a production incident: **XBlock handlers that block are a documented cause of gunicorn `WORKER TIMEOUT` in Open edX**, and the LMS worker pool is shared with courseware rendering — so a class of 200 using the tutor during a lecture would exhaust the pool and **take the LMS down for students who never opened the tutor.** Principle 8 forbids this. The topology is therefore explicit:

```
┌─ OPEN edX DEPLOYMENT ─────────────────────────────────────────────┐
│                                                                    │
│  LMS process (gunicorn)                                            │
│   ├── CourseMate XBlock — MINTS AND RENDERS, NEVER RELAYS:         │
│   │     student_view : render chat UI + seed it with history       │
│   │     json_handler : (a) mint short-lived JWT   → returns in ms  │
│   │                    (b) persist a completed turn to user_state  │
│   │     NO model calls · NO retrieval · NO embedding · NO RELAY    │
│   │                                                                │
│   └── LEARNING event receivers (enrollment / unenrollment)    (2)──┼──▶
│         thin: forward a cache-invalidation + scope notice          │
│                                                                    │
│  CMS process (Studio)                                              │
│   └── CONTENT_AUTHORING receivers (publish / delete /              │
│         import / rerun) — thin: validate + enqueue, return         │
│                    │ Celery                                        │
│                    ▼                                               │
│  CourseMate Ingest Worker  (runs INSIDE the platform — it must)    │
│   └── content_adapter → modulestore(published_only)                │
│       extract · chunk · embed · write                         (3)──┼──▶
└────────────────────────────────────────────────────────────────────┘
                                                                     │
  STUDENT'S BROWSER ── (1) ──▶ same-origin path /coursemate/ ────────┤
    holds the JWT, streams the answer, never touches an LMS worker   │
                                                                     │
   (1) student traffic  — signed JWT, SSE, hard timeout,             │
                          circuit breaker → "tutor unavailable"      │
   (2) invalidation     — service credential                         │
   (3) index writes     — service credential                         │
                                                                     ▼
┌─ COURSEMATE SERVICE (separate container, scaled and failed independently) ┐
│   ├── Student API (verifies the JWT on every request)     ◀── (1)         │
│   ├── Invalidation API                                    ◀── (2)         │
│   ├── Ingest write API                                    ◀── (3)         │
│   ├── CourseIntelligence interface ← in-process HERE                      │
│   ├── LangGraph agents · rewrite · rerank (CPU, self-hosted)              │
│   ├── Vector store · Metadata DB · 3 caches · proposal queue (dormant)    │
│   └── LiteLLM Router → cheap hosted + strong hosted models                │
└───────────────────────────────────────────────────────────────────────────┘
```

Four rules fall out, and they resolve the ambiguity completely:

1. **Content reading must happen inside the platform** — `modulestore()` is a Python API, not a network service. So the ingest worker is a Celery worker in the Open edX deployment. It reads in-platform and writes to our stores over the network.
2. **Reasoning must happen outside the platform** — so latency, memory, model failures and traffic spikes are contained in a container we can scale and restart without touching the LMS.
3. **The XBlock mints a token; the browser does the talking** *(changed in v8 — it was "the XBlock is a thin proxy")*. The `json_handler` issues a short-lived JWT and returns in milliseconds. The **browser** then opens the streaming connection to the CourseMate service directly. No LMS process is in the answer path at all.

   **Why the proxy shape was wrong, and it is the same bug as the one this section was written to fix.** A proxy that streams **holds the connection open for the entire generation** — five to fifteen seconds per answer. It holds no CPU, but gunicorn's pool is exhausted by **occupancy**, not by computation: two hundred students streaming concurrently is two hundred occupied workers, and the class-of-200 incident above returns unchanged. "The XBlock holds no work" was true of *computation* and false of *connections*, and only the second one is what the worker pool actually counts. Minting instead of relaying makes the claim true in the sense that matters — an LMS worker is held for a token mint, not for an answer.

   **What moves with it.** Rate limiting, the hard timeout and the circuit breaker move from the XBlock to the service, where §10.8 already wanted them — *"enforced at the boundary alongside authorization, so a new agent node cannot bypass them."* The XBlock keeps two jobs: render the chat UI seeded with history (§3.1), and persist a completed turn back to `Scope.user_state`. Both are millisecond handlers.

   **The cost, stated rather than buried:** the browser must be able to reach the service, which appears to contradict "not internet-exposed" below. It does not, because of *how* it is exposed — see the next block.
4. **Receivers are needed in *both* processes, because the events we depend on fire in both** *(corrected in v5)*. `content_authoring` events (`XBLOCK_PUBLISHED`, `XBLOCK_DELETED`, `COURSE_IMPORT_COMPLETED`, `COURSE_RERUN_COMPLETED`) fire in **Studio**. `learning` events — notably **`COURSE_UNENROLLMENT_COMPLETED`** (`org.openedx.learning.*`) — fire in the **LMS**. An earlier draft placed all receivers in the CMS, which would have left enrollment changes unobserved: the metadata cache (§6.4) invalidates on enrollment, and §10.7 scopes personal data on unenrollment. Both depend on an LMS-side receiver that posts an invalidation notice to the service.

**Authenticating the student hop** *(specified in v5; the caller changed in v8 from the XBlock to the browser, the mechanism did not)*. This is the one hop Open edX does not secure for us, so it is stated explicitly rather than assumed:

- The XBlock mints a **short-lived JWT**, signed with a key shared with the service, carrying `user_id`, `course_id`, roles, and an expiry of **minutes** *(widened from seconds in v8 — the token now has to outlive a conversation turn rather than a single server-to-server call; it is refreshed by another handler call when it expires, and the expiry stays short enough that a leaked token is worth little)*. The service **verifies the signature and expiry on every request** — it never trusts a caller-supplied identity.
- **The service is still not published as its own host.** It is exposed **only as a path under the LMS origin** — `https://lms.example.edu/coursemate/` — routed at the reverse proxy or ingress. Three consequences, and the middle one is the point: the browser sees **same-origin**, so there is no CORS surface and no third-party-cookie problem; the request is routed **by the ingress, never through an LMS application process**, so no gunicorn worker is involved; and the service has **no separately reachable hostname**, so nothing about the network posture is loosened beyond the one path that has to be reachable. Ingest, invalidation and admin routes are **not** exposed on that path — they stay on the private network.
- **The service re-derives authorization itself** — the JWT establishes *who is asking*; enrollment and role are re-checked at the boundary against the platform (§6.5, §10.1). A forged or replayed claim of enrollment therefore buys nothing. This mattered before and matters more now, because the token is handled by the browser: it is a **claim of identity, never a grant of access**.
- **Chat history is sent by the browser but owned by the platform** (§3.1). The student can therefore see and tamper with their own history — which changes nothing, because it is *their* conversation and the tampered version reaches only them. What it cannot do is widen retrieval scope: history feeds the rewrite node, and §8.1 already forbids the rewrite from widening the tenant/student/enrollment filter, which is applied afterward at the boundary.
- Ingest-write and invalidation APIs use a **separate service credential** from the student-facing API, so a leaked student-path token cannot write to the index.

### 3.5 Tenancy — one model, stated once (contradiction resolved in v4)

Earlier drafts held two incompatible models at the same time: a central *"AI Platform"* serving University A, B and Company C from per-tenant namespaces, **and** in-process modulestore reads inside the LMS. Both cannot be true — each university runs its own Open edX deployment, and you cannot read another organisation's modulestore across the internet.

**The MVP is a per-instance plugin. One deployment, one tenant.**

- `tenant` **stays in the schema** (§6.2) and in cache keys, because it costs nothing now and retrofitting an isolation key later is expensive. In the MVP it holds a single constant value.
- **Multi-tenant SaaS is deferred**, and it is deferred for reasons bigger than effort: it requires a different content-access path (no in-process reads), cross-instance authentication, data-residency answers per institution, and a per-tenant key-management story. Those are open questions, not a backlog item.
- Per-**student** isolation is *not* deferred — that is live from day one and is the boundary that actually matters in the MVP (§6.3).

### 3.6 Open items to verify on a running instance (honest)

> **These are scheduled, not merely noted.** `Week1_Verification_Plan.md` turns each into a bounded test with a stated setup, the design decision it gates, and what to do for every possible answer. They run in week 1 (§1.3), because an unexpected result costs nothing then and a great deal in week 3. Results get written back here as facts.
>
> **As of v7 the list is four open questions plus one confirmation** — the branch-setting item below was answered from platform source rather than by experiment, which is the cheaper way to close one of these when it is available. *`Week1_Verification_Plan.md` still describes it as an open gate and needs the matching edit (§17 maintenance rule).*
- Exact return shape of `get_item` per block type (clean text vs. a descriptor to render).
- ~~Whether a Celery worker inherits the branch setting or must pin `published_only` explicitly.~~ **Resolved from source in v7 — this is no longer an open question.** `MixedModuleStore.branch_setting` is a context manager that writes to **thread-local** storage (`self.thread_cache.branch_setting`) and restores the prior value on exit; with no active context the value is `None`. A Celery worker runs in a different thread with no context, so it **inherits nothing** and falls back to the store's own default — draft-preferred for the draft-capable stores. So the answer is settled: **pin `published_only` explicitly, always.** The earlier instinct ("we pin it regardless") was right; it is now a verified fact rather than a precaution.

  **The design consequence is bigger than the flag, and it is the reason this mattered:** `content_adapter` must **own** the branch context internally rather than expose it to callers. An API shaped as *"call `iter_leaves()` inside a `branch_setting` block"* fails open — one forgotten `with` silently indexes draft content, with no exception, no failing test, and no symptom until a student is cited unpublished text. Every content-reading function in the adapter opens the context itself; none is callable outside it.

  **Still worth 15 minutes in week 1**, but as a confirmation rather than a gate: assert on the target release that a queued task reading a block with unpublished draft edits returns the *published* text. If it ever returns the draft, that is a platform regression, not a design question.
- Whether a large **course import** fires per-block publish events, one event, or none. Both extremes are bad and both are handled (§5.4), but the actual behaviour determines which path runs.
- **Meilisearch:** whether the deployed index has an embedder configured, and how a plugin obtains a permission-scoped API key plus the exact index/field names. Only the *lexical half* of hybrid retrieval depends on these answers.
- Whether publishing a container in the Studio UI can be scoped to exclude specific children (the modulestore publish API accepts a blacklist; the UI's behaviour is what matters for §9.1).

---

## 4. Architecture — the layers

**Five layers, and two facts about their shape that the diagram alone does not convey:**

| # | Layer | Runs where | In the request path? |
|---|---|---|---|
| 1 | **Integration** — thin XBlock, event receivers, ingest worker | **Inside Open edX** | **No** — the XBlock mints a token and renders; it is not in the answer path (§3.4 r3) |
| 2 | **Ingestion** — bootstrap, incremental, chunk, embed, write-swap | CourseMate service, background | **No** — runs on publish, not on question |
| 3 | **Knowledge** — vector store, metadata, caches, isolation | CourseMate service | Yes |
| 4 | **AI** — boundary, rewrite, retrieve, rerank, generate, guards | CourseMate service | Yes |
| 5 | **Evaluation** — retrieval, answer, refusal and practice scoring | CourseMate service, offline | **No** — never runs while a student waits |

**Layer 1 is the only part inside the customer's platform, and it is deliberately the thinnest** (§3.4) — **and since v8 it is out of the answer path entirely**, since the XBlock mints a token rather than relaying the answer. **Layer 5 sits beside the pipeline, not at the end of it** — it measures layers 2–4 offline. Reading the stack as a straight left-to-right chain would imply evaluation costs a student latency; it does not.

```
┌──────────────────────────────────────────────────────────────────┐
│  OPEN edX (source of truth: content, permissions, progress; events)│
└───────┬──────────────────────────────────────────┬───────────────┘
        │ events (thin receiver → Celery)           │ XBlock: mint + render
┌───────▼────────────────┐            ┌─────────────▼─────────────┐
│  INGEST WORKER         │            │  SURFACE (XBlock/Aside)   │
│  in-platform, async    │            │  chat UI inside a lesson  │
│  bootstrap + increment │            └─────────────┬─────────────┘
└───────┬────────────────┘                          │ browser → HTTPS/SSE
        │                                           │ (same-origin path, §3.4)
        │                    ┌───────────────────────┘
┌───────▼────────────────────▼─────────────────────────┐
│  KNOWLEDGE LAYER                                      │
│   • our Vector store (semantic) + Metadata store      │
│   • platform Meilisearch keyword index (hybrid, later)│
│   • cache tiers: embedding · response · metadata      │
│   • course namespace + per-student upload namespace   │
└───────┬───────────────────────────────────────────────┘
        │ exposed ONLY through
┌───────▼───────────────────────────────────────────────┐
│  COURSE-INTELLIGENCE BOUNDARY                         │
│  scoped, audited, READ-ONLY tools                     │
│  MVP: in-process interface (inside CourseMate svc)    │
│  Later: same contract over MCP                        │
└───────┬───────────────────────────────────────────────┘
        │
┌───────▼───────────────────────────────────────────────┐
│  REASONING LAYER (LangGraph supervisor)               │
│  rewrite → hybrid retrieve → merge → rerank →         │
│  LLM(s) → cite / confidence guard                      │
│  + planner & quiz-generator nodes (exam prep)         │
└───────┬───────────────────────────────────────────────┘
        │ proposals only (never direct writes)
┌───────▼───────────────────────────────────────────────┐
│  PROPOSAL QUEUE → human accept → course (§9)          │
│  (designed; dormant in the MVP — the AI writes        │
│   no course content, §1.2)                            │
└───────────────────────────────────────────────────────┘

        EVALUATION LAYER (5) sits beside all of the above,
        not below it — offline, never in the request path.
```

---

## 5. Ingestion layer

### 5.1 Bootstrap — how content that already exists gets indexed (FIXED in v4)

**The bug this fixes:** ingestion used to be triggered *only* by `XBLOCK_PUBLISHED`. Install CourseMate on a running Open edX, open a course published six months ago, and the tutor answers *"not covered in this course"* for every question — because no event ever fired for that content and nobody re-publishes an old course to wake up a plugin. The confidence guard (§8.5) would make this look like correct behaviour, which is worse. This is the most likely way a demo fails.

**The fix, following the platform's own precedent.** Open edX solved exactly this problem for its own Meilisearch index: a one-time `reindex_studio` management command populates the index, after which incremental handlers keep it current (verified: `openedx.core.djangoapps.content.search` ships `reindex_studio`, with `--incremental`, and the docs state that after the one-time run *"it should not be necessary to run it again; from that point forward the indexes will be updated automatically"*). We follow the same shape:

```
manage.py cms coursemate_reindex --course <course_key>   # one course
manage.py cms coursemate_reindex --all [--incremental]   # everything
```

- Walks the **published** tree via `content_adapter.iter_leaves()`, batches, embeds, writes.
- **Idempotent** — safe to re-run; `--incremental` skips `usage_key@version` pairs already indexed.
- Emits progress and a **final count reconciled against the course tree**, so a partial run is visible rather than assumed complete.
- Is the recovery tool when reconciliation (§5.4) finds drift.

**What actually triggers it** *(specified in v5 — "runs automatically when enabled" was hand-waving, and there is no plugin-enabled event to hang it on)*. Three concrete entry points, in order of who uses them:

1. **A button in the block's Studio view** — *"Index this course for the tutor,"* showing last-indexed time and block count. This is the normal path, it lives in a surface that already exists (§9.2), and it puts the action in front of the person who just added the block.
2. **The management command**, for operators and for bulk enablement across many courses.
3. **Empty-index detection at query time** — if a student asks and the course has no index, the tutor answers *"this course is still being prepared — please try again shortly"* and **enqueues the bootstrap job**, rather than falsely reporting "not covered in this course." This is the backstop that makes the demo-killer non-fatal even if steps 1 and 2 were both missed: the difference between *looks broken* and *tells you what's happening*.

   **Guarded, because this is a student-triggerable expensive job** *(added in v5 — the backstop as first drafted let anyone queue a full course re-index by refreshing the page)*. Three controls: an **in-flight lock** so at most one bootstrap per course exists at a time and repeat requests attach to it rather than queueing another; a **cooldown** so a failed run can't be re-triggered in a tight loop; and the **per-course cost ceiling** from §10.8 applied to this path explicitly. The student-facing message is identical whether their request started the job or joined one already running.

The event pipeline is now the **incremental** path on top of a real initial state, not the only path.

### 5.2 Incremental trigger — thin receiver, async worker (FIXED in v4)

**The bug this fixes:** `openedx-events` signals are Django signals (verified: `OpenEdxPublicSignal` subclasses Django's `Signal`), so a `@receiver` runs **synchronously, in-process, inside the request that published the content** — and since `XBLOCK_PUBLISHED` is `org.openedx.content_authoring.*`, that request is in **Studio**. Running extract → chunk → **embed** inline therefore hangs the instructor's *Publish* button on third-party network I/O. Publishing a section with 40 leaves = 40 embedding round-trips inside the request. If the embedding provider is slow, **Publish is slow; if it's down, Publish fails.** That makes a core platform action depend on our vendor's uptime — a direct violation of Principle 8.

**The fix is again the platform's own pattern** (verified: `content.search` pairs `handlers.py` signal handlers with a `tasks.py` "asynchronous celery task for content indexing"):

```
@receiver(XBLOCK_PUBLISHED)          ← runs in the CMS request. Does ONE thing:
    validate → enqueue Celery task(usage_key, version) → return immediately

Celery worker (out of the request path):
    RESOLVE TO LEAVES   ← event names the published CONTAINER, not the changed
       │                  leaves. Walk descendants to html/problem blocks.
    VALIDATE            ← authorized, supported block type? else skip + log
    DEDUP               ← already indexed usage_key@version? skip
    EXTRACT → CHUNK → EMBED   (embedding cache absorbs unchanged leaves)
    WRITE-THEN-SWAP     ← §5.3
```

**Failure handling, which was entirely missing:** the task carries **retry with exponential backoff** on transient errors, a **dead-letter queue** for permanent ones, and every failure is logged with its `usage_key`. A block that fails all retries is recorded in a `failed_ingestions` table that the reconciliation sweep (§5.4) reports on — so a gap is *detectable*, never silent (Principle 8).

### 5.3 Write-then-swap, not delete-then-insert (FIXED in v4)

**The bug this fixes:** the v3 rule was *"delete all prior chunks for this `usage_key`, then extract → chunk → embed → write."* Embedding is a network call. If it times out or the worker dies mid-pipeline, the old chunks are already gone and nothing replaced them. A published lesson that students can see becomes **permanently invisible to the tutor**, and the tutor reports it as "not covered" — a data-loss bug that disguises itself as correct behaviour. Even on the happy path there is a window where the lesson is unsearchable.

**The corrected algorithm:**

```
1. WRITE   new chunks under usage_key@new_version   (old version untouched)
2. VERIFY  expected chunk count present and readable
3. SWAP    flip the active-version pointer for usage_key  ← atomic
4. GC      delete chunks for superseded versions (background, safe to retry)
```

Retrieval always filters on the **active version**, so stale content can never coexist with current content — which was the original rule's actual goal. The difference is that *failure now leaves the previous good state intact* instead of leaving a hole. Any step can fail and be retried without data loss.

The same principle applies at course scale for full rebuilds: build into a temporary index and **swap atomically** (Meilisearch supports index swapping for exactly this zero-downtime pattern), rather than resetting the live index.

### 5.4 The full content lifecycle — not just publish (FIXED in v4)

v3 handled publish and nothing else. The verified event inventory changes what's possible:

| Change | Platform event | Our handling |
|---|---|---|
| Publish | `XBLOCK_PUBLISHED` ✅ | Resolve to leaves, re-ingest (§5.2) |
| Block deleted | `XBLOCK_DELETED` ✅ | Drop all chunks for that `usage_key` subtree |
| Block duplicated | `XBLOCK_DUPLICATED` ✅ | Ingest the new `usage_key` |
| **Block created / edited in draft** | **`XBLOCK_CREATED`, `XBLOCK_UPDATED`** ✅ | **Deliberately not subscribed — see below** |
| **Course imported** | **`COURSE_IMPORT_COMPLETED`** ✅ | **One scoped bulk re-index** — not thousands of per-block events |
| **Course rerun** | **`COURSE_RERUN_COMPLETED`** ✅ | Bulk index the new course key |
| Unenrollment *(LMS-side receiver, §3.4)* | `COURSE_UNENROLLMENT_COMPLETED` ✅ | Scope/delete personal data (§10.7) + invalidate the permission cache |
| **Unpublish** | **none exists** ❌ | **Reconciliation sweep — see below** |

Note the unenrollment row is a **`learning` event, which fires in the LMS**, while the rest are `content_authoring` events firing in Studio — which is why receivers exist in both processes (§3.4, rule 4).

**Why `XBLOCK_CREATED` and `XBLOCK_UPDATED` are listed only to be refused** *(added in v7)*. They exist, they fire on every authoring action, and an earlier version of this table omitted them entirely — which is the more dangerous form of an error, because an unexplained absence reads as an oversight and invites the next engineer to wire them up. **They fire on draft edits.** Subscribing to them would index unpublished content and violate Principle 3 directly, with no error and no symptom until the tutor cites something students cannot see.

This is worth stating precisely because **the platform's own search app does subscribe to them** — `content/search/handlers.py` listens to `XBLOCK_CREATED`/`UPDATED`/`DELETED` and not to `XBLOCK_PUBLISHED`, since **Studio search deliberately indexes drafts**. So the §5.2 claim that we mirror `content.search` holds for its *async pattern* and **not** for its *event choice*; we diverge, and we diverge on purpose. One consequence is worth carrying honestly: there is **no in-tree consumer of `XBLOCK_PUBLISHED`** in edx-platform, so its firing behaviour is less exercised than the rest of this table, which raises rather than lowers the value of verifying it on a running instance (§3.6).

**Import and rerun were an unbounded-cost hole.** A 500-block course import could either flood the pipeline with events (a thundering herd and an uncapped embedding bill) or fire none (a silently empty index). Because `COURSE_IMPORT_COMPLETED` and `COURSE_RERUN_COMPLETED` exist, we subscribe to those and run **one** budgeted bulk index instead — with a per-course cost ceiling that pauses and alerts rather than spending without limit.

**Deferral consequence, stated per the §1.2 rule:** these two handlers are designed but not built in the delivery window. Until they are, an imported or rerun course is indexed **only when someone runs the bootstrap command or clicks the Studio button** (§5.1) — the index is not wrong, it is *absent*, and the query-time backstop tells the student so rather than claiming the content doesn't exist. That is an acceptable gap precisely because the bootstrap path exists; it would not have been acceptable as the only path.

**Unpublish is a real, unfixable-by-events gap, and we say so.** There is **no unpublish event** in `openedx-events`. So if an instructor unpublishes a unit, nothing tells us, and the tutor would keep teaching from — and citing — content students can no longer see. That is a direct Principle 3 violation and the kind of thing a reviewer will simply *try*.

Mitigation, stated with its limitation:

> A **periodic reconciliation sweep** walks each indexed course's published tree and compares the set of live `usage_key@version` pairs against the index. Orphans (unpublished or deleted) are removed; missing blocks are queued for ingestion; blocks in `failed_ingestions` are retried and reported. It runs nightly, and on demand.
>
> **This leaves a window** — up to one sweep interval — during which the tutor may cite unpublished content. We shorten it by also running the sweep for a course on its next publish event. We do not claim it is closed, because without a platform event it cannot be.

### 5.5 Chunking — structure first, token count second
Three ordered criteria:

1. **Open edX block boundaries are authoritative.** A leaf block is a unit the instructor deliberately authored as one idea — and it is our citation key and swap key. **Two blocks are never merged into one chunk**, because that chunk could no longer cite a single `usage_key`.
2. **Semantic boundaries within a block** — headings, list groups, worked examples, code fences, problem stem vs. solution. A definition is never split from its term; a worked example never from its problem statement.
3. **Token range as a guard rail** — ~512–1024 tokens (top of current benchmarks; recursive 512-token splitting ranks first among common strategies), **no default overlap** (overlap adds indexing cost without measurable benefit), staying under the ~2,500-token quality cliff. Short blocks stay whole; over-long segments split at the next-best semantic boundary.

**In one line:** token count decides *where within a semantic unit we are forced to split*; it never decides *what a unit is*.

---

## 6. Knowledge layer

### 6.1 Retrieval strategy — reuse the platform's search, add the semantic layer it lacks
**Verified:** Open edX ships **Meilisearch** as core infrastructure (Tutor auto-provisions it since Sumac) and maintains a published-content index with permission-aware access. Its `index_config.py` defines the classic keyword ranking pipeline (`words, typo, proximity, attribute, exactness`) and **no vector field**; the module contains no references to `embedder`, `vector`, `semantic`, or `hybrid`.

**Caveat, stated honestly:** in Meilisearch an embedder is enabled via a settings/API call, not necessarily in that static config, so its absence there is a **strong signal, not proof**.

**Our decision — hybrid by composition:** two complementary retrievers, merged. **Lexical** = the platform's Meilisearch keyword index (exact terms, permission-scoped candidate selection). **Semantic** = our own vector store. The justification holds either way: if the platform has no embedder, our layer supplies semantic search; if a release enables one, our layer collapses into it behind the same interface.

**MVP scope (per §1.2):** semantic + metadata filters ship first; the Meilisearch lexical half is designed and deferred, because it depends on an unverified integration seam (permission-scoped API key from a plugin). The system is fully functional without it.

### 6.2 Metadata schema
Retrieval **filters on metadata before ranking** — that is what makes permission-scoping, versioning and CLO-targeting possible.

| Group | Fields |
|---|---|
| **Identity** | `tenant`, `course_id`, `offering_id`, `usage_key`, `block_id`, `block_type` |
| **Versioning** | `version`, `active` (the swap pointer, §5.3), `course_version`, `publish_time`, `updated_at` |
| **Nature** | `content_type` (`lesson`\|`problem`\|`transcript`\|`slide`\|`past_paper`\|`clo_doc`\|`student_note`), `language` |
| **Targeting** | `CLO`, `week`, `topic` |
| **Isolation** | `student` (personal uploads only) |

The three time/version fields are not redundant: `publish_time` is about the *content*, `version`/`course_version` about *which revision*, `updated_at` about *our pipeline*. Debugging a stale answer needs all three.

### 6.3 Two isolation boundaries
Per-**tenant** namespace for course content (single-valued in the MVP, §3.5); per-**student** private namespace for personal uploads — so one student's past paper never surfaces in another's answers. Enforced by metadata filters applied *before* ranking, so unauthorized content is never a candidate rather than merely never returned.

### 6.4 Cache tiers
Three caches with distinct keys and invalidation rules. They fail differently — a stale response cache is a **correctness/security** bug; a stale embedding cache is merely a **cost** miss.

| Tier | Key | Invalidated by |
|---|---|---|
| **Embedding** | `hash(chunk_text) + embedding_model_id` | Content-addressed, never stale; LRU eviction |
| **Response** | `tenant + offering_id + course_version + hash(effective_permission_scope + applied_filters) + normalized_rewritten_query + mode` | Any `course_version` bump; TTL ceiling |
| **Metadata** | `student_id + course_id` (permissions); `offering_id` (CLO map) | Enrollment events; CLO edits; **short** TTL |

Three things worth flagging:

- **The embedding cache pairs with verified platform behaviour.** Because a publish event fires for the *container*, a section-level publish forces re-resolution of every leaf in that section, most unchanged. Dedup on `usage_key@version` skips most; the embedding cache catches the rest. Together they turn a section publish from an O(section) re-embed into O(changed leaves).
- **The response-cache key includes the effective permission scope** *(fixed in v4)*. Keying only on the query string meant a **course-staff** member's answer — retrieved from a wider candidate set — could be served to a **student** who asked the same question. The key now hashes the caller's effective scope and applied filters alongside the query.
- **Personal-namespace results are never cached** — not stored, not served. A cache is the classic way isolation quietly fails *after* all the filters are written correctly. This is a security control (§10.2), not an optimization. The metadata cache is deliberately the shortest-lived, because a *revoked* enrollment must stop working quickly.

### 6.5 The Course-Intelligence boundary — which seam is in-process (clarified in v4)

The v3 review asked, correctly: *"If your only consumer is LangGraph, why not call retrieval directly?"* The answer separates the **boundary** from the **protocol**. v4 adds the piece that was missing — *which* boundary, and where it sits, now that the topology is explicit (§3.4). There are **two distinct seams**, and v3 conflated them:

| Seam | Nature | MVP implementation |
|---|---|---|
| **Browser → CourseMate service** *(v8: was XBlock → service)* | **A network boundary from day one** — required by §3.4, non-negotiable | Authenticated HTTPS/SSE on a same-origin path, our own API |
| **Agents → Knowledge layer** | An in-process seam *inside* the CourseMate service | `CourseIntelligence` Python interface |

So "in-process" never meant "inside the LMS" — it means inside the reasoning service, which is itself out-of-process from Open edX. This also answers the round-2 objection that the promotion trigger *"when the reasoning layer needs to run out-of-process"* had already fired: it has, and it is satisfied by the student-facing API. What remains deferred is only whether the **agent→data** hop speaks MCP.

**Why the inner boundary earns its place on day one — security, not extensibility.** Four things must happen on *every* data access: resolve identity, check enrollment/role for the requested scope, apply the tenant/student filter **before** ranking, write an audit record. Scattered across agent nodes, a new node can forget one. Behind a single interface they are a chokepoint that cannot be bypassed. That argument holds with exactly one consumer.

**The four tools** — each scoped to the current student + enrollment, audit-logged, and **read-only**:
- `retrieve_course_context(query, offering_id, student_id)`
- `get_student_progress(student_id, offering_id)`
- `get_struggle_signals(offering_id)` — aggregate, anonymized, k-anonymity floor (§10.3)
- `get_exam_prep_pack(offering_id, student_id)`

*Every tool is keyed on **`offering_id`**, not `course_id` — corrected in v5 for consistency with §7.4. The offering is the real isolation unit (University A's CS-101, Fall 2026 holds a different Exam Prep Pack and a different cohort from the same course run a year later), and mixing the two keys would have been the kind of subtle scoping bug that only shows up once a second run of the same course exists. The boundary resolves `course_id` → `offering_id` from the caller's enrollment.*

**Promotion trigger (written down):** expose it over MCP when a second consumer appears — a Studio-side authoring assistant, an instructor dashboard, or an external/IDE client. The tool signatures, authz checks, audit records and return schemas already *are* the MCP contract; promotion is wiring, not rework.

---

## 7. Solving the exam-prep data problem

**The problem:** a university course's **CLOs, past papers, and slides do not live in Open edX**, vary institution to institution, and often arrive as messy or scanned files.

### 7.1 The core insight
You don't *source* this data centrally — **the person who legitimately holds it provides it, and we structure the upload.** That converts an unbounded sourcing question into a bounded ingestion workflow.

### 7.2 Who provides the data (three paths, priority order)
1. **Instructor / course team (best).** CLO list + past papers + slides uploaded into a course-owned **Exam Prep Pack**, shared to all enrolled students in that offering.
2. **Student (fallback).** Their own past papers/slides into their **private per-student namespace**, used only for them.
3. **Open datasets (development/demo only).** MIT OpenCourseWare ships lecture notes, problem sets and past exams — see the licensing constraint in §7.7, which is stricter than v3 implied.

**MVP scope, per §1.2:** the delivery window covers **one pre-loaded pack** — the storage, extraction, question schema and namespacing are all real, but the *self-serve upload UIs* for both instructor (path 1) and student (path 2) are deferred, and path 3 is how the demo is populated. The consequence is narrow and worth naming: the feature is fully functional for a pack that has been loaded, and there is no way for a user to load one themselves yet. Nothing about the design changes when those UIs land — they write to the same pipeline.

### 7.3 In what form, and how we handle each
Route by type — one tool does not handle every file type:
- **Digital PDF / DOCX** → direct text + layout extraction (reliable).
- **Scanned / photographed** → **layout-aware / vision-language parser**. 2026 parsers (Google Document AI Layout Parser, Mistral OCR, Azure Document Intelligence) understand layout, reading order and tables. **Handwriting remains the weak point** — flagged best-effort, never guaranteed.
- **CLO document** → **schema-guided LLM extraction with confidence scoring** (structured JSON-schema output with field-by-field validation outperforms single-pass extraction), then **human-confirmed** before it becomes the spine. CLO mapping is *assisted*, never asserted.

### 7.4 Per-offering namespaces
Each **course offering** (University A's CS-101, Fall 2026) gets its own namespace keyed by `offering_id`. New university = new namespace, **no custom code**.

### 7.5 Tagging past papers to CLOs
Each extracted question is embedded and **tagged to its nearest CLO** — AI-proposed, correctable by instructor or student.

### 7.6 The past-paper question schema — a question is a record, not a blob
Extracting only question *text* throws away the structure the feature runs on.

| Field | Source | Notes |
|---|---|---|
| `question_id` | generated | Stable ID for mastery tracking and dedup |
| `offering_id`, `tenant` | upload context | Isolation |
| `source_doc_id`, `page`, `question_number` | extraction | Provenance — every generated item traces to a real paper |
| **`year`** | paper header | "Only the last 3 years" — filters out a syllabus that has since changed |
| **`exam_type`** | paper header | `mid`\|`final`\|`quiz`\|`assignment` — a finals plan weights *final* papers |
| **`marks`** | printed on the question | Proxy for depth and time; drives plan realism and mirrors the real paper's shape |
| **`difficulty`** | *derived, estimated* | From marks + command verb (Bloom level) + model estimate. **Always labelled derived and correctable** — never presented as printed on the paper |
| `topic`, `clo_id` | extraction / AI-tagged | The spine (§7.5) |
| `confidence`, `extraction_method`, `low_confidence_flag` | pipeline | `digital`\|`ocr`\|`vlm`; low-confidence items shown as such |

**What this unlocks** — the query the feature actually exists for:

> *"Give me practice for **CLO-3**, from **final** papers, **2023 onward**, worth **10+ marks**, that I haven't mastered."*

A metadata filter over structured records, not a semantic search over blobs. Study plans are sized by a **marks budget** (a 2-hour session is 100 marks, not "8 questions"), and mastery tracks per `clo_id` × difficulty band.

### 7.7 Where exam-prep data lives, and the licensing constraint (RESOLVED in v4)

**The contradiction v3 carried:** §7.2 said an instructor's pack "becomes course content," while the architecture doc said uploads live in our object storage, **not** in Open edX. Both were asserted. The decision:

> **All uploaded exam-prep material — instructor packs and student uploads alike — lives in CourseMate object storage, keyed by `offering_id` (and `student_id` for personal uploads). None of it is written into the Contentstore.**

Two reasons, and the first is a leak we would otherwise have built:

1. **Course exports would carry the exam bank.** Content in the Contentstore is included in the OLX export, and course exports are routinely shared between institutions. Storing past papers there builds a mechanism that leaks one university's exam bank to another. Keeping them out of the course package is the only safe option.
2. **The course data model can't hold the records.** §7.6 needs per-question structured fields; the Contentstore stores binary assets.

**Principle 1 still holds**, and this is the important nuance: *we are the storage, Open edX remains the permissions authority.* Access to a pack is decided by asking the platform who is enrolled in that offering with what role — we never maintain our own enrollment list.

**Licensing and IP — a constraint v3 didn't state.** Verified: MIT OpenCourseWare is **CC BY-NC-SA**. That imposes three hard rules:

- **Non-Commercial.** OCW material may be used for **development and demonstration only**. It must never be ingested into a paying institution's namespace, and if CourseMate is ever sold, OCW content cannot ship with it.
- **Share-Alike.** Derivative works must carry the same licence — which does not compose with Apache-2.0. So OCW content stays **data we index at demo time**, never material redistributed in the repository.
- **Attribution.** Demo material is credited to MIT and the author.

**Institutional past papers are institutional IP.** They stay in their offering's namespace, never cross a tenant boundary, never enter a course export, and are deleted with the offering. Who is entitled to upload them is a policy question for the institution — we enforce that only course staff can create a pack, and log who uploaded what (§10.5).

### 7.8 The exam-prep data flow
```
Provider (instructor / student / open dataset)
   → upload: CLO doc + past papers + slides  (PDF / DOCX / image)
   → route by type: digital → extract | scanned → VLM/OCR | CLO doc → schema extraction
   → human confirms CLO list  (AI-proposed → approved)
   → split papers into QUESTION RECORDS (§7.6)
   → chunk → embed → per-offering namespace (+ per-student private)
   → tag questions to CLOs  (AI-proposed → correctable)
   → exposed via get_exam_prep_pack()
```

### 7.9 Honest residual risks
- **Handwritten/low-quality scans** degrade — flagged best-effort; the pack shows low-confidence items.
- **CLO extraction/mapping can err** — human-confirm step; never silent.
- **Derived difficulty is an estimate** — labelled, correctable.
- **Instructor adoption** — path 1 depends on instructors uploading; paths 2 and 3 keep the feature alive without them.

---

## 8. Reasoning layer

### 8.1 Query rewriting — the step before retrieval
Raw student questions are conversational, elliptical and time-referential. The canonical failure:

> *"What about that algorithm from week 4?"*

Embedded as-is this retrieves nothing: there is no signal for "that algorithm," and "week 4" is **metadata, not content**. Every query therefore passes through a rewrite node first (cheap model, with chat history and the course outline available), emitting **two** outputs:

```
├──▶ query  : "Dijkstra's shortest path algorithm — complexity and correctness"
└──▶ filter : { week: 4, content_type: [lesson, problem] }
```

It resolves three things the raw query can't carry: **coreference** ("that algorithm" → the named entity), **temporal/structural references** ("week 4", "before the midterm" → a `week`/`publish_time` filter — which is why those fields exist in §6.2), and **multi-intent splitting**.

**Guard rails:** the rewrite is **schema-constrained** (query string + filter object — it cannot emit free-form instructions); it is **traced** (§11.4) so a bad rewrite is diagnosable rather than blamed on the retriever; it **cannot widen scope** (the tenant/student/enrollment filter is applied afterward at the boundary and cannot be overridden — "search all courses" gets you your own course); and a well-formed standalone question **passes through unchanged**, so exact technical terms survive for the lexical retriever.

### 8.2 Retrieval recipe and the latency budget

```
Question → REWRITE → hybrid search (lexical ∪ semantic) → merge ~20 → rerank → top 3–5 → LLM
```

Merge the two candidate sets, then **rerank with a cross-encoder** to the top 3–5 (converged 2026 practice: a cross-encoder's head-of-distribution carries the signal, so reranking 100+ rarely pays). All retrieval is metadata-filtered **before** ranking.

**The reranker is a named component with a home** *(gap closed in v4)*. It is not a LiteLLM call — it is a small cross-encoder (bge-reranker-base class) running **on CPU inside the CourseMate service**, ~100–300 ms for 20 pairs. No GPU at this scale. **Stated degradation mode:** under load or on failure, skip reranking and take top-k by merged hybrid score — measurably worse, explicitly logged, never an outage.

**Latency budget** *(absent in v3, and reviewers ask)* — target **p95 < 2 s to first token**, full answer streaming thereafter:

| Stage | Budget |
|---|---|
| Browser → service (same-origin path via ingress, incl. JWT verify) | 30 ms |
| Auth + boundary checks (metadata cache) | 50 ms |
| Rewrite (cheap hosted model) | 300 ms |
| Hybrid retrieve | 150 ms |
| Cross-encoder rerank | 250 ms |
| Generation — time to first token (hosted model) | 800 ms |
| **Total to first token** | **~1.6 s** |

Anything that would push this over budget must justify itself. That constraint is what forces the design in §8.5.

**Which models are self-hosted — and the answer is "one, the reranker"** *(corrected in v5)*. An earlier revision closed the "reranker has no home" gap and then quietly gave homes to two more components it hadn't costed: a self-hosted rewrite model and a self-hosted fallback `llama3`. **Three inference services is not a 3.5-week line item.** The corrected split:

| Component | Where it runs | Why |
|---|---|---|
| **Cross-encoder reranker** | **Self-hosted, CPU, in our service** | Small, no GPU at 20 pairs, and there's no sensible hosted equivalent in the budget |
| **Rewrite model** | **Cheap *hosted* model** | Short input, short output, schema-constrained — roughly $0.0002/query, which still rounds to ~0 in §12. Buys the 300 ms budget with **zero** infrastructure |
| **Fallback generation** | **Second *hosted* provider**; self-hosted local model **deferred** | The availability argument was never "the model is local" — it was "survive one vendor's outage." A different provider does that with a config line |

**What the deferral of the local model actually costs:** the chain becomes *strong hosted → retry → secondary hosted → honest failure* (§8.4). We lose the last rung — the case where **both** providers are down simultaneously — which is rarer than either being down alone, and is the rung that would have been slowest and least accurate anyway. Stated plainly rather than papered over: **in the MVP, a total outage of both hosted providers means the tutor is unavailable, and says so.**

*(For the record, had the local model shipped: on CPU it would not meet an 800 ms first token — several times that — so degraded mode would have been degraded in latency as well as quality, and the UI would have had to say so. That constraint is why it isn't the cheap win it looks like.)*

### 8.3 Orchestration: LangGraph supervisor
Multi-step features run on **LangGraph** with the **supervisor pattern** — the 2026 production default, with the best-understood failure mode (over-delegation, bounded by iteration caps). LangGraph is the orchestration layer, not the retrieval layer. **Cost caveat:** multi-agent orchestration can cost roughly 15× the tokens of a single chat, so the graph stays shallow (supervisor one level deep), routine steps go to the cheap model, and retries are capped.

### 8.4 Multi-model routing and the fallback chain
All calls go through the **LiteLLM Router**. Verified: the Router provides `fallbacks`, `content_policy_fallbacks`, `context_window_fallbacks`, a per-error-type `RetryPolicy` with exponential backoff, and `allowed_fails` **cooldowns** that remove an unhealthy deployment from the pool. **So this is configuration, not code** — which is what makes it affordable inside the delivery window.

Two models ship: a **cheap hosted** model for classification, rewriting and simple lookups, and a **strong hosted** model for explanation, Socratic dialogue and generation. Both are provider strings; swapping either is config.

```
   strong hosted model
        │ timeout / 5xx / rate-limit
        ▼  retry with backoff (RetryPolicy)
   secondary hosted model (different provider)   ← survives one vendor's outage
        │ unavailable  (cooldown trips: allowed_fails)
        ▼
   honest failure: "the tutor is unavailable right now"   ← never a fabricated answer
        ┊
        ┊ (deferred rung, §8.2: a self-hosted local model would sit here —
        ┊  labelled DEGRADED and visibly slower. Not shipped in the MVP,
        ┊  so a simultaneous outage of both providers means unavailable.)
```

Four rules keep the fallback from becoming a silent quality regression:
1. **Retry only what's retryable.** Timeouts/429/5xx retry. A **content-policy refusal routes to `content_policy_fallbacks`** rather than retrying; a malformed 400 fails fast.
2. **Cooldowns, not blind retries.** After `allowed_fails` the deployment leaves the pool for a cooling period instead of paying a full timeout on every request during an outage.
3. **Degradation is visible.** The trace always records which provider answered, and any answer from a fallback tier is labelled in the UI. Otherwise an outage reads as "the tutor got worse this week."
4. **Grounding never relaxes on fallback.** The fallback model gets the same context, the same citation requirement, the same τ. Falling back changes *who answers* — never *whether the answer must be grounded*.

### 8.5 Grounding, confidence, and verification that survives streaming

> **Grounded, cited, and confidence-aware.** Every answer is built from retrieved context and cites its source block. **If retrieval confidence falls below a calibrated threshold, the tutor abstains.**

This replaces v2's *"the tutor never fills gaps"* — a promise nobody can keep, since you cannot prove a model never draws on parametric memory. Three gates, cheapest first:

| Gate | Signal | Behaviour below bar | Cost |
|---|---|---|---|
| **Retrieval** | Top reranker score `< τ` | Abstain **before generating a token**: *"That doesn't appear to be covered in this course."* | free |
| **Weak evidence** | Score clears τ but margin is thin, or top chunks disagree | Answer, hedged and explicitly scoped | free |
| **Post-generation** | Claims no retrieved chunk supports | See below | *not free* |

**The streaming conflict, and the fix** *(v3 bug, closed in v4).* Gate 3 as written required the complete answer before showing anything — which rules out token streaming and blows the §8.2 latency budget. Students compare this to ChatGPT; a 12-second blank box reads as broken. The 2026 pattern is to **stream and verify in parallel**: stream tokens immediately, run verification concurrently, and surface a correction banner within ~500 ms of completion if a claim fails. So:

- **Stream by default.** Gate 1 has already run *before* generation, so nothing streams that failed the retrieval bar.
- **Verify in parallel, not in series.** Start with **string/semantic matching of each assertion against the retrieved chunks** — near-zero added latency, and it catches a meaningful share of unsupported claims. Escalate to a model-based check only when cheap matching is inconclusive.
- **Correct visibly.** A failed claim raises an inline flag on the answer with the unsupported sentence marked, rather than silently rewriting text the student already read.

**Why τ is defensible and where it isn't** *(honesty fix)*: τ is a number, calibrated on labelled data, and **both** error directions are measured — **false answers** (answered when it should have abstained) and **false abstentions**. But a 20–30 question pilot yields perhaps ~15 negatives, which **cannot** calibrate a threshold to any useful precision. So the honest statement is: *τ is **initialized** from the pilot and **refined** from logged production abstentions*, and the pilot reports a confidence interval rather than a point estimate. We tune toward abstention: a confidently wrong answer costs a student more than an unnecessary "not covered."

**Other guards.** Citation is mandatory — an answer that cannot cite abstains. Student chat is **untrusted**; published course content is **trusted**; uploaded documents are **semi-trusted data, never instructions** (§10.6). Socratic mode returns a guiding question first for conceptual asks and does **not** relax grounding — the guiding question is itself derived from retrieved content.

---

## 9. Human-in-the-loop: where approval actually happens

Three of this design's safety guarantees depend on a human acting. v3 assumed those humans without giving them a surface or a way to know there was anything to act on. The loop was drawn closed and wasn't.

### 9.0 What needs approval, and what doesn't (corrected in v5)

Earlier versions routed **everything** the AI generated through instructor approval — including a practice question generated for the one student who asked for it. That was wrong twice over. It made **Feature B undemonstrable** (generated practice could never reach the student, because approval sat behind a review UI that isn't in scope), and it confused two categories that carry completely different risk:

| | **Personal output** | **Course content** |
|---|---|---|
| **Examples** | A tutor answer; a practice question generated for the student who asked; a study plan | A new explanation added to a unit; a practice item added to the course for everyone |
| **Who sees it** | One student, once | Every enrolled student, persistently |
| **Governed by** | **Principle 4** — grounded, cited, confidence-aware, abstains below τ | **Principle 2** — proposal queue + explicit human accept (§9.1) |
| **Why that's right** | Requiring a human to approve one student's private study aid is unworkable *and* pointless — it reaches nobody else, and it is already bounded by the same grounding and citation rules as any tutor answer | It becomes part of the course, carries the institution's authority, and is seen by people who never asked for it |

**The line is "does anyone other than the asker see it?"** Personal output is ephemeral and attributed to the tutor; course content is durable and attributed to the course.

**One guarantee that does not weaken:** personal practice is still labelled as AI-generated, still cites the past paper or lesson it derives from (§7.6 provenance), and is still measured by the Feature B rubric (§11.3) — approval is not what was keeping it honest, grounding and measurement were.

**What crosses the line, and what doesn't ship.** Two paths could move an item from personal to course content: the AI proposing one from struggle signals, or a **student promoting one** ("ask my instructor to add this"). Both are **deferred with the rest of the instructor loop** (§1.2) — the MVP produces no course-content proposals from either direction. The queue schema carries an `origin` field (`ai_proposal` \| `student_request`) so that neither path needs a schema change when it lands. Flagging this because an earlier revision described promotion in a table and a flow diagram without a field, a surface, or a line in the scope table — which is the same "assumed a UI" error this section exists to correct.

### 9.1 The proposal queue — why AI content no longer goes to the draft branch

> **Scope note (v5):** this section is **designed and dormant**. Because the MVP generates no course content (§1.2), the queue has nothing to hold. It is specified in full anyway, for two reasons: it is the design's answer to a genuine platform hazard that any future content generation would hit immediately, and the hazard is worth documenting whether or not we exploit it. Read it as *"here is why this is safe when switched on,"* not as a description of week-8 behaviour.

**The bug this fixes, and it was the deepest one.** Principle 2 said the AI writes to the **draft** branch and an instructor reviews and publishes. But **Open edX publish semantics are subtree semantics** (verified against the Split modulestore design: publishing a node publishes its children to the destination, and children removed in the source are removed at the destination). So if the AI puts a proposed block into a unit's draft and the instructor later fixes an unrelated typo in that unit and hits Publish, **the AI's content goes live too** — as an invisible side effect of an unrelated action. The instructor never reviewed it and has no reason to suspect it exists.

The guarantee was enforced by *hoping the instructor noticed a new block*. That is not a gate.

**The fix inverts the flow — approval causes the write, rather than filtering it afterward:**

```
  AI drafts a fix (from struggle signals + completion)
        │
        ▼
  PROPOSAL QUEUE          ← CourseMate storage, OUTSIDE the course tree.
  (proposal_id,           ← Nothing here is reachable by any publish action.
   offering_id,           ← Zero risk of accidental exposure.
   target usage_key,
   origin: ai_proposal
         | student_request,
   content, rationale,
   evidence, status)
        │
        ▼  instructor opens the review surface (§9.2)
   ACCEPT ──▶ write to draft AND publish, in one atomic action
   REJECT ──▶ archived with reason (feeds quality tracking)
   EDIT   ──▶ instructor's edited version is what gets written
```

Because a proposal never touches the course tree until the moment it is accepted, **there is no state in which unreviewed AI content can be published by accident.** Principle 2 is now enforced by the storage location, not by attentiveness.

**The same bug, pointing the other way — and how accept handles it** *(found and fixed in v5)*. Subtree semantics do not stop being true at the moment of acceptance. If the target unit already contains **the instructor's own unpublished work-in-progress**, then "write to draft and publish" would publish *their* unfinished edits as a side effect of accepting an unrelated AI suggestion. The v4 fix closed AI→student and quietly re-opened instructor→student. Accept therefore **checks the target container for other pending draft changes before doing anything**:

```
ACCEPT(proposal)
   │
   ├── target container has NO other draft changes
   │      └──▶ write to draft → publish → done          (the common case)
   │
   └── target container HAS other pending draft changes
          └──▶ STOP and tell the instructor exactly what else would go live,
               then offer three explicit choices:
                 (a) publish only this proposal   ← scoped publish, excluding
                                                     their in-progress children
                 (b) publish everything, having seen the list
                 (c) cancel; the proposal stays queued
```

Option (a) is the one worth building: the modulestore publish API accepts a **blacklist**, which is exactly the mechanism for "publish this child, leave those alone." **We treat that as unverified (§3.6)** — if it turns out not to be usable from our position, the fallback is (b)/(c) only, and the instructor still cannot be surprised, because the list of what else would publish is shown before anything happens.

**The invariant, stated once:** *no publish caused by CourseMate ever makes content live that the instructor has not seen in that moment.* That covers both directions — AI content the instructor didn't review, and instructor drafts they didn't intend to ship.

### 9.2 The instructor surfaces, named and scoped

v3 assumed four instructor interactions and designed none of them. v5 names all four and ships the one the MVP actually needs:

| # | Interaction | Surface | Status |
|---|---|---|---|
| 1 | Configure the block; **"Index this course"** (§5.1) | Studio view of the CourseMate block | **Built** — the only instructor surface in the MVP |
| 2 | Confirm the extracted CLO list | Studio view | **Deferred** — in the MVP the pack is loaded via the command, and the CLO list is confirmed **at load time by the person running it**. The human-confirm guarantee (§7.3) holds; the *surface* moves, not the check |
| 3 | Upload / manage the Exam Prep Pack | Studio view | Deferred (§7.2) — pre-loaded pack only |
| 4 | Review AI proposals (accept / reject) | Studio view, backed by the §9.1 queue | Deferred with the whole instructor loop (§1.2) — nothing generates proposals in the MVP |
| 5 | Correct CLO tags and derived difficulty | Inline in the pack view | Deferred (§1.2) |

All live in the **Studio view of our own XBlock** rather than a new MFE — the cheapest surface inside a tool instructors already use, needing no Frontend Plugin Slot.

**Notification (deferred with #4).** A queue nobody looks at is the same as no queue, so when the loop lands it needs a **badge count** on the Studio view plus a **digest email** to course staff. Stated plainly for that future: if notification were cut, the loop's *latency* would degrade but its *safety* would not, because nothing publishes without an accept.

### 9.3 Where the struggle signal would come from (deferred, §1.2)
`get_struggle_signals` derives from the **Completion/Grades APIs** plus server-side aggregation in our own store — **not** from `Scope.user_state_summary` as the sole source. Reason: `user_state_summary` is shared-scope and writable from any student's request, so a counter kept there is manipulable by students and unreliable as an instructor-facing signal. Aggregation happens server-side where students cannot write to it, subject to the k=5 floor (§10.3).

**Two reasons this is deferred rather than built, and the second is the honest one.** First, it supports neither headline feature. Second — **the signal would be biased to the point of being misleading in the MVP**: the tutor is placed per-unit by the instructor, so "students are stuck on topic X" can only ever surface for units where a tutor was already added. An instructor would read absence of signal as absence of struggle. That resolves at the Aside stage, when coverage becomes automatic; shipping it before then would be shipping a metric that lies by omission.

**The k=5 floor leaves Built with it** — legitimately, per §1.2 rule 1: a control may be deferred *together with* the feature it guards. It returns the moment struggle signals do.

---

## 10. Security & privacy

**10.1 Authorization is inherited, never reinvented.** Open edX owns identity, roles and enrollment; we never define a parallel permission model. Every request carries the platform's authenticated identity, and the boundary (§6.5) re-checks enrollment and role **on every tool call**, not once per session. Revocation takes effect within the metadata cache's short TTL. Instructor-only tools check the course-staff role, not merely enrollment.

**10.2 Student isolation, three layers.** (a) Metadata filters on `tenant`/`student` applied **before** ranking, so unauthorized content is never a candidate; (b) per-student namespaces for personal uploads; (c) **the response cache never stores or serves anything whose retrieval touched a personal namespace**, and its key includes the effective permission scope (§6.4). Layer (c) exists because caching is how isolation quietly fails after (a) and (b) are correct.

**10.3 Aggregates that can't de-anonymize.** "2 of 4 students are stuck on X" identifies people. `get_struggle_signals` enforces a **k-anonymity floor (k = 5 distinct students)**: below the floor the signal is suppressed entirely rather than rounded or bucketed. Small cohorts are common in university courses, so this is a real case, not a theoretical one.

**10.4 No raw API keys inside courses — enforced by construction, not by an export filter** *(corrected in v7)*. The earlier claim was that instructor-configurable settings live in `Scope.settings` with *"keys excluded from course export."* **No such mechanism exists.** Settings-scoped fields are precisely what OLX serialises, and there is no standard XBlock feature that marks one non-exportable. The cited precedent does something different: `open-craft/xblock-ai-evaluation` reads a key at the XBlock level and *falls back to Site Configuration* — the protection is "keep the key somewhere else," not "hide the field from export."

For CourseMate the correct answer is stronger than the one it replaces, because the topology already gives it to us for free:

> **The XBlock holds no credentials at all.** It mints a token and renders a UI (§3.4 rule 3); it makes no model calls, so it has no reason to know a provider key. Every credential lives in the CourseMate service, from environment/secret storage. The JWT signing key comes from Django settings, never from a field.

`Scope.settings` therefore carries only non-secret configuration — enabled, mode, display name — and an exported OLX carries no credential because **there was never a credential in the block to export**. This is the same move §1.2 makes for Principle 2: a guarantee satisfied by construction rather than by a control that has to work. Keys are never written into OLX, never logged, never returned by any tool.

**10.5 Encryption and provenance.** TLS on every hop. At rest: server-side encryption on object storage, the metadata database and the vector store. Uploaded PDFs are the sensitive artifact — a student's or an institution's private documents. Every upload records who uploaded it and when (§7.7).

**10.6 Prompt-injection defence.** Three trust tiers: **untrusted** student chat (delimited, role-separated, schema-constrained rewriting that cannot widen scope); **semi-trusted** uploaded documents — the real injection vector, since a PDF's text lands in the model's context, so retrieved document text is always framed as **quoted data, never directives**; **trusted** published course content. **The strongest mitigation is structural: the agent's entire tool surface is read-only, and the only path into a course runs through the proposal queue and a human accept (§9.1). There is no prompt that makes CourseMate change what students see.**

**10.7 Data lifecycle and deletion** *(extended in v4)*. A student's uploads are theirs: they can list and delete them, and deletion cascades from the raw object to chunks, embeddings, extracted question records and cached entries by `source_doc_id`. Beyond student-initiated deletion, we consume platform lifecycle events so our stores don't outlive the platform's copy:

- **`COURSE_UNENROLLMENT_COMPLETED`** (verified to exist) → scope down or purge that student's course-linked personal data.
- **Course deletion** → drop the offering's namespace, including the Exam Prep Pack.
- **User retirement.** Verified: Open edX's retirement feature is a **configurable pipeline of building-block APIs**, with the LMS authoritative, explicitly designed to call out to **external services holding PII**. CourseMate is exactly such a service. We therefore expose a **retirement endpoint** (delete all data for a user ID, idempotent, returning a confirmation) and document registering it as a pipeline state. *No retirement **event** exists in `openedx-events`, so this is an API integration, not a receiver.* **Status: designed, deferred (§1.2)** — but the deletion API it needs is built, because student-initiated deletion uses the same path.

**10.8 Abuse and cost controls.** Per-student rate limits and a per-tenant token budget, enforced **at the boundary alongside authorization** so a new agent node cannot bypass them. Bulk ingestion (import/rerun, §5.4) carries a separate per-course cost ceiling that pauses and alerts rather than spending without limit.

---

## 11. Evaluation

### 11.1 Score both layers, not just the answer
Measuring only the final answer hides retrieval failures — a documented case: a legal RAG scored 0.91 faithfulness while missing a key statute 1-in-6 times; only context recall (0.62) exposed the retriever. So we score **retrieval** (context precision, context recall) *and* **generation** (faithfulness, answer relevancy) separately — the four canonical Ragas metrics, reference-free, where faithfulness = supported claims / total claims.

### 11.2 Automated *and* human
Ragas is an **LLM-as-judge proxy** — fast and repeatable, but a model grading a model, which does not answer *"who says the answer is good?"*

- **(a) Automated, every run:** 20–30 questions × 4 metrics, faithfulness floor ~0.85.
- **(b) Human rating** — scoring *correctness*, *groundedness*, **citation validity** (does the link point at the lesson that really contains this?), and **pedagogical usefulness** (would this help someone learn? — a correct, grounded, useless answer still fails as a tutor). **Two tiers, and only one of them ships:**
  - **Ships:** a **single-rater assessment** over the pilot set, with *single rater* named as the limitation. Weak against rater bias; still the only thing that catches a fluent, well-cited, useless answer.
  - **Deferred (§1.2):** the **blind two-rater study** with inter-rater agreement — the version that removes the author from the loop.
- **(c) Abstention audit:** a deliberately mixed set of covered and uncovered questions, scoring false answers and false abstentions — the data that informs τ (§8.5).

**Human ratings are ground truth; Ragas is validated against them.** If the two diverge on the pilot set, the automated metric is the one that is wrong — and with a single rater, that comparison is *indicative, not conclusive*, which is exactly how it gets reported.

*(Why the split exists: an earlier revision deferred "human rating" wholesale while shipping the Feature B rubric — whose four dimensions are all human judgements. That left a stated control with nobody performing it. A single rater is a real limitation; zero raters was an inconsistency.)*

### 11.3 Feature B needs its own rubric (gap closed in v4)
v3's entire evaluation measured the tutor. **Feature B shipped with no quality gate at all** — and a wrong *generated practice question* is more damaging than a wrong answer, because students study from it and may sit an exam having practised something false.

A 30-item sample of generated practice, rated on four dimensions — **single rater in the MVP, per §11.2(b)**, since all four are human judgements and there is no automated proxy for any of them:

| Dimension | Check |
|---|---|
| **Validity** | Is it a well-formed, answerable question? |
| **CLO alignment** | Does it actually test the CLO it's tagged to? |
| **Provenance** | Does it trace to a real past-paper pattern, or was it invented? |
| **Difficulty calibration** | Does the derived difficulty match a human's judgement? |

**This rubric carries more weight than it did in v4, and that is deliberate.** Once personal practice reaches the student without an instructor gate (§9.0), measurement *is* the control — there is no human backstop between a generated question and the student studying from it. Instructor approval still gates anything entering the course, but for the personal path the honest position is: **the rubric plus grounding and provenance are what make this safe, so the rubric ships with the feature** (§1.2). Shipping Feature B without it would have meant shipping an unmeasured, ungated output.

### 11.4 Observability
Every run is traced end to end: the **rewritten query and its emitted filters**, each retrieval and its candidate scores, the rerank, which model answered (including fallback tier), token counts, the confidence decision, and cache hit/miss per tier. Failures localize to a *stage* rather than a vague "the tutor was wrong."

---

## 12. Cost and capacity (NEW in v4)

An AI feature is a spend endpoint. Order-of-magnitude figures with assumptions named — prices move, the shape doesn't.

**Ingestion is effectively free, which is the surprise.** A large course ≈ 500 leaf blocks × ~800 tokens ≈ **400 K tokens**. At a commodity embedding rate of **~$0.02–0.13 per million tokens**, that is **roughly $0.01–0.05 per course, one time** — and re-publishes touch only changed leaves (§5.2). Ingestion cost is not the thing to worry about. It still gets a ceiling, because a bulk import of many courses multiplies it without a human intending to (§10.8).

**Answering is where the money goes.** Assumed rates: **~$3 / M input, ~$15 / M output** for the strong hosted model (name your own — the arithmetic is shown so it can be re-run when prices move).

| Item | Assumption | Estimate |
|---|---|---|
| Rewrite | cheap hosted model, short in/out | ~**$0.0002** — rounds to 0 |
| Rerank | CPU cross-encoder, self-hosted | ~0 marginal |
| Generation | ~4 K input (3–5 chunks + history) + ~500 output | 4 K×$3/M + 0.5 K×$15/M ≈ **$0.02 / question** |
| Student, one term | 20 questions | **~$0.40** |
| Course of 200 | 4 000 questions | **~$80 / course / term** |

**Sensitivity, because "20 questions a term" is the number a reviewer will push on** — and rightly, since a tutor that sits in every lesson invites far heavier use:

| Questions / student / term | Per student | Course of 200 |
|---|---|---|
| 20 (conservative) | $0.40 | **$80** |
| 50 (moderate) | $1.00 | **$200** |
| 100 (heavy — tutor becomes a habit) | $2.00 | **$400** |

Even the heavy case is **~$2 per student per term**, which is the useful conclusion: at these volumes the per-student cost stays small, and the things that actually threaten the budget are structural, not volumetric —

- **A deep agent graph multiplies everything ~15×** ($0.30/question → **$1 200/course** at 20 questions, **$6 000** at 100). This is the concrete reason §8.3 keeps the supervisor shallow and routes routine steps to the cheap model. The constraint has a price tag.
- **Exam week is a spike, not a plateau.** Traffic concentrates into days, so the ceiling that matters is per-day, not per-term. The response cache (§6.4) earns its keep exactly then — 200 students asking overlapping questions against identical content — and the per-tenant budget and rate limits (§10.8) bound the worst case.

**Capacity.** The CourseMate service scales horizontally and independently of the LMS (§3.4) — which is the point of the topology. The LMS is unaffected by tutor load because **it is not in the answer path at all**: it mints a token and is released, so tutor traffic scales against CourseMate replicas rather than against the gunicorn pool *(v8 — under the earlier proxy shape this claim held for CPU but not for worker occupancy, which is the resource that actually runs out)*.

---

## 13. Feature flows

### 13.1 Tutor answers (Feature A)
```
Student asks "What about that algorithm from week 4?"
  → XBlock json_handler: mint short-lived JWT, return (LMS worker freed in ms)
  → browser opens the stream to the service on the same-origin path (§3.4 r3)
  → service: verify JWT, rate-limit
  → boundary: enrollment/role check + audit
  → REWRITE (cheap model + history) → query + {week: 4} filter
  → retrieve_course_context (filters applied BEFORE ranking)
  → semantic (+ lexical, later) → merge ~20 → cross-encoder rerank → top 3–5
  → confidence gate: below τ? → abstain ("not covered in this course")
  → LangGraph supervisor → tutor node → strong LLM (LiteLLM + fallback chain)
  → STREAM answer; verify claims in parallel; flag unsupported ones
  → mandatory citation to source block   (Socratic optional)
  → audit log written
```

### 13.2 Final Exam Prep (Feature B)
```
Student clicks "Prepare me for finals"
  → boundary: get_exam_prep_pack(offering_id, student_id)
  → planner node → study plan organized BY CLO,
       sized by MARKS BUDGET, weighted toward exam_type=final and recent years
  → quiz-generator node → practice filtered by (clo_id, difficulty, marks, year)
  → PERSONAL output → shown to this student directly (§9.0)
       labelled AI-generated · cites the past paper it derives from
       NO instructor gate — it reaches nobody else
       measured by the Feature B rubric (§11.3), which IS the control here
  → track mastery per CLO × difficulty band → target weak CLOs

  (deferred, §1.2: "ask my instructor to add this to the course"
   → PROPOSAL QUEUE, origin=student_request → instructor accept (§9.1))
```

### 13.3 Instructor safety loop *(designed; deferred from the MVP per §1.2)*
```
"Concept X: 60% stuck"  (completion + server-side aggregation, k≥5, §10.3)
  → AI drafts a fix → PROPOSAL QUEUE (outside the course tree)
  → badge + digest email to course staff
  → instructor reviews → ACCEPT
       → conflict check: does this unit hold the instructor's own draft work?
            no  → write to draft → publish
            yes → show what else would go live → publish-only-this / all / cancel
  → publish event → incremental re-ingestion  (loop closes)
```

---

## 14. Technology choices

| Concern | Choice | Why |
|--------|--------|-----|
| Surface | XBlock (mints + renders) → Aside filtered to `vertical` | Verified extension points; `should_apply_to_block` prevents N tutors per page |
| Topology | XBlock mints a JWT → **browser** → HTTPS/SSE → CourseMate service on a same-origin path; ingest worker in-platform | Blocking XBlock handlers cause gunicorn worker timeouts — and a *streaming proxy* occupies a worker just as effectively as a blocking one (§3.4 r3) |
| Content read | `modulestore()`, `published_only`, behind `content_adapter.py` | Verified API + branch guarantee; adapter absorbs future storage change |
| Bootstrap | `coursemate_reindex` management command | Mirrors the platform's own `reindex_studio`; without it, existing courses are invisible |
| Ingestion | Thin signal receiver → Celery task | Mirrors `content.search` handlers.py + tasks.py; never blocks Publish |
| Index writes | Write → verify → **swap** → GC | Delete-first loses content permanently on a mid-pipeline failure |
| Lifecycle | Publish/delete/duplicate/import/rerun events + nightly reconciliation; receivers in **both** LMS and CMS | **No unpublish event exists** — reconciliation is the only mitigation, so it ships. Enrollment events are `learning` events firing in the LMS |
| Student hop | Short-lived signed JWT; service exposed only as a path under the LMS origin; authz re-derived server-side | The one hop the platform doesn't secure for us; a path keeps it same-origin without publishing a second host |
| Conversation state | Owned by the platform in `Scope.user_state`; browser carries a rolling window and posts the completed turn back through the XBlock; service is stateless | Keeps "the platform keeps chat private" literally true; no PII duplicated outside retirement's reach |
| Chunking | Block boundary → semantic boundary → ~512–1024 token guard rail | Structure is the citation key; token count constrains, never defines |
| Knowledge | Vector store + metadata + 3 caches; Meilisearch lexical half later | Platform index is keyword-oriented (verified); embedder unconfirmed |
| Cache keys | Include effective permission scope; personal-namespace results never cached | Caching is how isolation quietly fails |
| Query handling | Schema-constrained rewrite → query + metadata filter | "Week 4" is a filter, not a vector |
| Reranker | CPU cross-encoder in our service; skip-under-load degradation | Not a LiteLLM call — it needs its own home and budget |
| Data access | Read-only boundary, authz + audit; in-process **inside CourseMate**; MCP on 2nd consumer | Security chokepoint at one consumer; transport deferred |
| Human loop | **Proposal queue outside the course tree**; accept shows what else would publish before acting | Publish is subtree semantics — which breaks draft-parking *and* naive accept-and-publish |
| Approval scope | Course content gated; **personal output governed by grounding + measurement instead** | Gating one student's private study aid is unworkable and protects nobody |
| Exam-prep storage | CourseMate object storage by `offering_id`; never Contentstore | Course exports would leak the exam bank between institutions |
| Licensing | OCW = CC BY-NC-SA → demo/dev only, never commercial, never redistributed | Non-Commercial + Share-Alike are hard constraints |
| Models | LiteLLM Router: `fallbacks`, `content_policy_fallbacks`, `RetryPolicy`, `allowed_fails` cooldown | Verified native features — config, not code |
| Model hosting | **One** self-hosted service (the CPU reranker); rewrite + generation + fallback all hosted | Three inference services is not a 3.5-week line item; failover needs a second *provider*, not a local model |
| MVP content generation | **None.** The AI produces answers and personal study material only | Principle 2 satisfied by construction rather than by a review UI — and it removes four subsystems from the build |
| Grounding | Cite always; abstain below τ; **stream + verify in parallel** | Serial post-generation checking is incompatible with streaming |
| Evaluation | Ragas + human rubric + abstention audit + **Feature B rubric** | A wrong generated question is worse than a wrong answer |

---

## 15. Risk register

**Fixed in v4 (previously broken):**
1. ~~No path to index existing courses~~ → bootstrap command (§5.1).
2. ~~`delete-then-insert` loses content on failure~~ → write-then-swap (§5.3).
3. ~~Synchronous receiver blocks Publish~~ → thin receiver + Celery (§5.2).
4. ~~Draft-parking is not an approval gate~~ → proposal queue (§9.1).
5. ~~LLM work could run in LMS workers~~ → explicit topology (§3.4).
6. ~~SaaS and in-process reads both asserted~~ → per-instance MVP (§3.5).
7. ~~Instructor Exam Pack storage undefined~~ → our storage, never Contentstore (§7.7).
8. ~~Aside would render N tutors per page~~ → `should_apply_to_block` at `vertical` (§3.1).
9. ~~Post-generation gate blocked streaming~~ → parallel verification (§8.5).
10. ~~Cache key allowed a staff→student leak~~ → permission scope in the key (§6.4).

**Fixed in v5 (introduced by v4's own fixes, or left open by them):**
11. ~~Accept-and-publish would sweep up the instructor's unfinished drafts~~ → conflict check + scoped publish (§9.1).
12. ~~Personal practice routed through instructor approval, making Feature B undemonstrable~~ → §9.0 separates personal output from course content.
13. ~~Receivers only in the CMS, missing LMS-side enrollment events~~ → receivers in both (§3.4 rule 4).
14. ~~Cut line silently cancelled three stated guarantees~~ → sweep, Feature B rubric and k=5 floor moved into Built; deferrals that narrow a claim must say so (§1.2).
15. ~~Chat history had no defined home~~ → platform-owned, service stateless (§3.1).
16. ~~XBlock→service hop unauthenticated in the spec~~ → signed short-lived JWT + private network, authz re-derived (§3.4).
17. ~~Bootstrap trigger hand-waved~~ → Studio button + command + query-time backstop (§5.1).
18. ~~Fallback model had no home or latency figure~~ → resolved by not shipping it; one self-hosted service, hosted failover instead (§8.2, §8.4).
19. ~~The scope table grew while arguing for scope discipline~~ → instructor loop deferred entirely; Built is smaller than in v4 (§1.2).
20. ~~Three self-hosted inference services implied~~ → one (§8.2).
21. ~~Feature B rubric shipped with its raters deferred~~ → single-rater assessment ships, blind two-rater study deferred (§11.2b).
22. ~~"Promote to course" existed only in a diagram~~ → `origin` field in the queue schema; the path itself deferred with the loop (§9.0).
23. ~~Query-time bootstrap was a student-triggerable expensive job~~ → in-flight lock + cooldown + cost ceiling (§5.1).

**Fixed in v8 (found while sequencing the build):**
40. ~~The thin-proxy XBlock would hold an LMS worker for the whole answer stream, recreating the exact worker-exhaustion incident §3.4 was written to prevent~~ → the XBlock mints a JWT and the browser connects directly (§3.4 rule 3). *This one is worth remembering as a pattern: the v4 fix moved the **computation** out of the LMS and then measured the result in CPU, while the pool that runs out is measured in **connections**. A guarantee stated in the wrong unit reads as satisfied when it isn't.*

**Live risks, named honestly:**
24. **Unpublished content can be cited until the next reconciliation sweep** — no platform event exists; the sweep ships (§1.2) and runs nightly plus on publish, but the window is real and stated (§5.4).
25. **Hallucination reduced, not eliminated** — grounding + citation + abstention + measured faithfulness; the residual is reported as a number.
26. **τ cannot be properly calibrated at pilot scale** — initialized from ~15 negatives, refined in production, reported with a confidence interval (§8.5).
27. **Personal practice has no human gate** — deliberate (§9.0), and the reason the Feature B rubric stays in Built: for that path, measurement *is* the control.
28. **Evaluation is single-rater in the MVP** — the author is in the loop; blind two-rater agreement is deferred, and that limitation is published with the numbers (§11.2b).
29. **A simultaneous outage of both hosted providers makes the tutor unavailable** — the local-model rung is deferred (§8.4). Rarer than either provider failing alone, and it fails honestly rather than degrading silently.
30. **`get_item` return shape and worker branch context unverified** — on the "test on Tutor first" list (§3.6).
31. **Scoped publish (accept-with-blacklist) unverified** — matters only when the instructor loop lands; accept then falls back to publish-all-after-showing-the-list or cancel, and the instructor is never surprised either way (§9.1).
32. **Import/rerun event behaviour unverified**, and the handlers are deferred — until then, imported courses need a manual bootstrap (§5.4).
33. **Meilisearch integration seam unverified** — the lexical half is deferred; the system works semantic-only (§6.1).
34. **Multi-agent cost (~15×)** — shallow supervisor, cheap routing, caches, retry caps, budgets (§12).
35. **OCR on handwritten/low-quality scans degrades** — best-effort with low-confidence flagging.
36. **CLO extraction/mapping and derived difficulty can err** — AI-proposed, human-confirmed at pack-load time, never silent (§9.2).
37. **Exam prep depends on a pack existing** — three provider paths designed, one pre-loaded in the MVP, with OCW constrained to non-commercial demo use (§7.7).
38. **The instructor loop is absent, so there is no improvement feedback in the MVP** — deliberate (§9.3): the signal would have been biased by tutor placement, and a metric that misleads by omission is worse than none.
39. **Scope vs. timeline** — addressed structurally by the Built/Designed cut line (§1.2), which in this version got *smaller*, not larger.

---

## 16. One-paragraph summary

CourseMate is an AI layer on Open edX delivering a **course-grounded, cited, confidence-aware tutor** and a **Final Exam Prep mode** organized around a course's CLOs and past papers. It attaches through verified extension points as a **thin XBlock that mints a short-lived token and lets the browser stream from a separate service**, so no LMS worker is occupied by an answer — not by the model call, and not by the connection carrying it; content is read **in-platform** by an **asynchronous Celery ingest worker** pinned to the published branch behind a storage adapter, triggered by a **bootstrap command plus publish events** — because an event-only design leaves every pre-existing course invisible — and written **write-then-swap** so a mid-pipeline failure can never delete a lesson from the index. Chunks follow **block and semantic boundaries**, carry a metadata schema rich enough to filter by version, time, type, language and CLO, and are reachable only through a **read-only Course-Intelligence boundary** that checks enrollment and writes an audit record on every call. The exam-prep data problem is solved by a **provider-uploads model** with **per-offering namespaces** and **structured question records** (marks, year, exam type, CLO, derived difficulty), stored in our own object storage — never the Contentstore, because course exports would leak an institution's exam bank. Answers come from a **rewrite → hybrid retrieve → rerank** pipeline on a shallow LangGraph supervisor across a cheap local and a strong hosted model via the LiteLLM Router's native fallback and cooldown configuration, **abstaining below a calibrated threshold** and **streaming while verifying in parallel** so safety doesn't cost latency. **No AI-generated *course content* — anything durable, or seen by students who didn't ask for it — reaches anyone without an explicit human accept**, while personal output (a tutor answer, a practice question for the student who requested it) is governed by grounding and measurement instead, since gating one student's private study aid helps nobody. Proposals live **outside the course tree**, because Open edX publish is subtree semantics: content parked in draft can be published by accident, and by the same token accepting a proposal must never sweep up an instructor's own unfinished edits — so accept shows exactly what would go live before anything does. Quality is measured by Ragas, an abstention audit, a rubric for generated practice questions, and human raters at milestones. It never modifies the core, never blocks a platform action, never teaches from unpublished content, and states plainly which subsystems are designed but not built in the delivery window — and which guarantees would narrow if any of them slipped.

---

## 17. Document set

This design is the technical source of truth. The other documents are derived from it and must not contradict it — if they do, this one wins and the other is stale.

| Document | Audience | Purpose |
|---|---|---|
| **`CourseMate_Complete_Design.md`** *(this)* | Engineers building it | Every decision with its reason, its cost, and the alternative rejected |
| `CourseMate_Project_Brief.md` / `.docx` / `.pdf` | Decision-makers, managers | The proposal: why, goals and non-goals, approach, alternatives, scope, risks, ask. Send the PDF |
| `CourseMate_Repository_Structure.md` | Whoever writes the code | Repository and folder layout, with each boundary traced to the decision here that requires it; plus the source-verification log behind v7 |
| `CourseMate_Build_Plan.md` | Whoever builds it, and whoever checks on it | Day-level sequence, four milestones, the pre-committed cut ladder, and the build risks distinct from §15's product risks |
| `OpenedX_Architecture_Analysis_v2.md` | Engineers new to Open edX | How the platform works — what each part owns, where data lives, how the pieces connect |
| `Architecture_Review_Round2.md` | Reviewers | Four rounds of adversarial review: every fault found, its evidence, its fix. This is the evidence behind "seven faults found" |
| `Week1_Verification_Plan.md` | Whoever runs week 1 | The five open platform behaviours as bounded tests, with the design decision each one gates |

**`archive/`** holds superseded material kept only for history: the earlier 35-page proposal (now split between the Brief and this document), the plain-language plan, the mentor Q&A, the round-1 review response, the presentation guide and the original review notes. Nothing current depends on any of it.

**Maintenance rule, learned the hard way across four review rounds:** when something moves from *built* to *deferred*, search every document for its name before closing the change. Four separate inconsistencies in this set were caused by a claim outliving the thing that supported it — a differentiator that had been cut, a control for a feature no longer shipping, a fallback model no longer used. Each was cheap to fix and would have been expensive to be caught on.

**The rule runs in both directions, which the 2026-08-12 fold-back proved.** Things moving from *deferred* to *built* strand claims just as effectively, and they are harder to spot because a stale "not built" reads as modesty rather than as an error. Applying the search found four:

| Claim | Where | Why it was false |
|---|---|---|
| "exam prep" listed under **Not built** | `TECHNICAL_SUMMARY.md` | Built, deployed and browser-verified |
| "producing that JSON from real papers is manual" | `LIMITATIONS.md` §5.2, §8 | `tools/extract/` does it; measured end to end |
| "automated CLO tagging — the prompt exists, nothing calls it" | `LIMITATIONS.md` §5.2 | `ai/clo_tagger.py` calls it |
| "no hosted provider has been exercised" | `LIMITATIONS.md` §2 | Contradicted by §5.2 *in the same file* — a Groq run was documented there |

The last one is the instructive case: the contradiction was **internal to one document**, and survived because each section was edited on its own. Searching by feature name rather than re-reading top to bottom is what surfaced it.

### 17.1 Fold-back record — 2026-08-12

What the design now describes as **built and verified**, against §7 (exam prep), §3.4 (service split) and §6.5 (the boundary):

* **§7.6 past-paper records** — real PDF → `tools/extract/extract_pack.py` (pypdf, digital text) → structured `QuestionRecord`s with marks, page and provenance. OCR/VLM for scanned papers remains deferred, as the scope table above always said.
* **§7.3 CLO tagging, assisted never asserted** — `ai/clo_tagger.py`, offline batch, on the cheap deployment. A refusal is the safe outcome; an out-of-scope outcome id is refused rather than coerced.
* **§7.4 marks-budgeted study plan** — `ai/planner.py`, deterministic, no model call, exposed at `POST /examprep/study-plan`.
* **§9.0 personal practice generation** — labelled, cited and measured, reaching a student with no instructor gate exactly as the design argued it could.
* **§3.4 / invariant 1** — the *request path* confirmed under a real browser: the network trace shows `handler/mint` returning a JWT, then the browser calling `/coursemate/api/examprep/*` directly through the ingress. That establishes the LMS is not in the answer path; it is not a fresh measurement of worker occupancy, which was measured separately and earlier (3 concurrent generations, zero LMS log lines, 103 ms LMS CPU).

Still deferred, unchanged: the instructor loop (§9.3), XBlockAside (§3.1), Meilisearch hybrid (§6.1), multi-tenancy (§3.5), retirement-pipeline registration (§10.7), and difficulty calibration — the last of which is why one rubric metric cannot be measured on a real pack.
