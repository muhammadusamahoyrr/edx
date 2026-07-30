# Open edX — Full Architecture & Constructor Analysis (Revised)

*A repo-by-repo breakdown of how the platform works: what each part is, where it gets its data, in what form, and how the pieces coordinate — written as the foundation for an AI layer (AI Tutor + Final Exam Prep) built on top of Open edX.*

*Revision note: this version scopes storage claims to the deployment model being targeted, reframes internal-access choices as tradeoffs, adds explicit draft/publish and ingestion-safeguard states to the workflows, and specifies the previously-vague student-upload and vector subsystems.*

---

## 0. Architectural principles (read these first)

Three principles govern every decision below. They are stated up front because they are the trust foundation the rest of the document builds on.

**Principle 1 — Open edX is the source of truth; the AI never modifies the core.** Courses, permissions, and progress are owned by the platform. Our AI is added *only* through the platform's sanctioned extension points (XBlocks, the Hooks/Events framework, and — later — Frontend Plugin Slots). We never patch or fork the core codebase. Everything that follows is an *extension*, not a modification.

**Principle 2 — The AI proposes; a human approves and publishes.** Whenever the AI would change course content, it writes only to the **draft** branch. An instructor reviews it in Studio and publishes it. The AI never writes to the published branch and never auto-publishes. The platform's immutable versioning gives audit and rollback for free.

**Principle 3 — The AI only ever learns from *published, authorized* content.** Ingestion is triggered by a publish event, scoped by enrollment/permissions, and (for personal uploads) isolated per student. Draft, unpublished, or unauthorized content never reaches the tutor.

Everything else in this document is implementation detail in service of these three principles.

---

## 1. Scope of this architecture (what deployment model we assume)

Open edX storage is **actively evolving**, so this document states which model it targets rather than presenting one as universal or permanent:

- **Today's common model:** most current Open edX deployments store course content in the **Split Mongo Modulestore** (MongoDB-backed, versioned, draft/published branches). **This architecture assumes that deployment model.**
- **The direction of travel (not assumed, but designed around):** the community is evolving content storage away from MongoDB. Two facts we can state: **Content Libraries v2 is backed by Blockstore rather than the modulestore**, and the community's stated direction is toward **Learning Core** (`openedx-learning`, a Django-ORM model). **We deliberately assert no schedule, sequence, or end state for that evolution** — it is in progress and owned by the community, and a design that bakes in a predicted migration path ages badly the moment the community picks a different one.

**Design consequence, stated schedule-free:**

> **Open edX is evolving toward Learning Core. This design isolates content access behind an adapter to remain compatible with future storage changes.**

Concretely: we depend on **stable platform APIs and events**, not on database internals, and every content read goes through one adapter module. Where we read content, we read it through platform services/runtime rather than assuming a specific database engine — so a storage change touches that one module, whatever shape the change turns out to take.

---

## 2. The big picture (how the layers fit)

```
                    ┌───────────────────────────────────────────┐
                    │              OPEN edX PLATFORM             │
                    │                                            │
                    │  ┌──────────────┐      ┌──────────────┐    │
                    │  │   STUDIO     │      │     LMS      │    │
                    │  │  (authoring) │─────▶│  (delivery)  │    │
                    │  │   = CMS      │ pub- │              │    │
                    │  └──────┬───────┘ lish └──────┬───────┘    │
                    │         │ writes             │ reads       │
                    │   ┌─────▼─────────────────────▼──────┐     │
                    │   │        DATA (current model)      │     │
                    │   │  Content store: Split Mongo      │     │
                    │   │    Modulestore (platform is      │     │
                    │   │    evolving toward Learning Core)│     │
                    │   │  Files: Contentstore / GridFS    │     │
                    │   │  MySQL: users, enrollment, grades│     │
                    │   │  Search index: course search     │     │
                    │   └──────────────────────────────────┘     │
                    │                                            │
                    │  Emits EVENTS (openedx-events) on publish  │
                    │                                            │
                    │  IN-PLATFORM part of our layer:            │
                    │   • thin XBlock (auth + proxy only)        │
                    │   • thin event receiver → Celery           │
                    │   • INGEST WORKER (reads modulestore)      │
                    └──────────────┬─────────────────────────────┘
                                   │ HTTPS  (never blocking calls
                                   │         inside a web worker)
                    ┌──────────────▼─────────────────────────────┐
                    │   OUR AI SERVICE (separate container)      │
                    │   Vector + Metadata → boundary → agents    │
                    │   scaled / restarted independently         │
                    └────────────────────────────────────────────┘
```

**The split matters (see design doc §3.4).** Content reading *must* happen inside the platform — `modulestore()` is a Python API, not a network service — so the ingest worker lives in the Open edX deployment. Everything else *must* happen outside it: LLM calls take seconds, and blocking XBlock handlers are a documented cause of gunicorn `WORKER TIMEOUT`. Since the LMS worker pool is shared with courseware rendering, a busy class using the tutor would otherwise degrade the site **for students who never opened it**. The XBlock is therefore a proxy — authenticate, rate-limit, forward, with a hard timeout and circuit breaker — not an application.

Three ways our AI talks to the platform, each for the job it does best:

1. **Internal runtime (inside the LMS/CMS):** our **ingest worker** runs in-platform, so it can read *published* content through the platform's runtime/services. We use **direct modulestore access only where a service path doesn't exist**, and treat that as a coupling tradeoff to minimize (see 4.3). Note this is the *worker*, not the web request.
2. **Events (the event bus):** the platform emits a signal when content is **published** (not merely edited). Best for the "notify me on change" trigger — but see §5.1: events alone never cover content published before install.
3. **REST APIs (authenticated):** Course Blocks, Enrollment, Grades/Completion. Best for progress data and anything read from outside the platform process.

---

## 3. MVP → Phase-2 progression (how the surfacing evolves)

The tutor's *UI surface* evolves in stages. This is drawn explicitly so scope is unambiguous:

```
  STAGE 1 (MVP)            STAGE 2                    PHASE 2 (deferred)
 ┌───────────────┐       ┌────────────────┐         ┌──────────────────────┐
 │ Tutor as an   │       │ Same engine as │         │ Course-wide floating │
 │ XBlock:       │  ───▶ │ an XBlockAside:│  ─────▶  │ tutor via Frontend   │
 │ per-unit,     │       │ auto-appears   │         │ Plugin Slot          │
 │ teacher adds  │       │ on every lesson│         │ (persistent chat)    │
 │ it to a unit  │       │ (no editing)   │         │                      │
 └───────────────┘       └────────────────┘         └──────────────────────┘
   proven, low risk        native, still XBlock       heavier + platform
                                                       maturity caveat ↓
```

**Why Phase 2 is deferred — the honest, platform-based reason:** the course-wide floating tutor needs the **Frontend Plugin Slot** system, which is still **maturing** — relatively few slots are currently exposed across the MFEs and the tooling isn't fully seamless yet. Deferring it is a *platform-readiness* decision, not merely "it's a bigger build." The AI **engine** is identical across all three stages; only the attachment surface changes, so nothing is wasted by starting with the XBlock.

---

## 4. Repo-by-repo constructor analysis

For each: **what it is → what data it owns / where it lives → in what form → how it hands off.**

### 4.1 `openedx/edx-platform` (the core monolith: LMS + Studio)

- **What it is.** The central application, run in two modes from one codebase: **Studio (CMS)** for authoring and **LMS** for delivery. Django backend; some legacy screens still render via Mako/Django templates as the platform migrates to React MFEs.
- **What data / where (current model).** The front door to the data stores: course content in the **Split Mongo Modulestore** (migrating toward Learning Core); uploaded files in **Contentstore / GridFS**; users, enrollment, grades, completion in **MySQL**; course search in a search index.
- **In what form.** Content as versioned XBlock field data (JSON in Mongo today); files as binary blobs in GridFS; learner data as relational rows.
- **How it hands off.** Studio writes to a **draft** branch; publishing promotes it to the **published** branch the LMS reads. It emits **events** (4.4) and exposes **REST APIs** (4.5). Our AI reads the published branch only.

### 4.2 `openedx/XBlock` (the component framework)

- **What it is.** The framework for building course components (video, quiz, HTML — and our tutor). A course is a tree of XBlocks.
- **What data / where — the Scope system (our privacy mechanism).** Every field declares a **Scope** deciding who owns it and where it's stored:
  - `Scope.content` — shared course content.
  - `Scope.settings` — instructor config (prompts, model choice, keys kept out of exports).
  - `Scope.user_state` — **one user, one block** → private per-student data (conversation memory, per-student progress). *This is where the tutor's chat history lives, and it stays there: our reasoning service is stateless and receives a rolling window with each request, so the platform remains the owner of the conversation.*
  - `Scope.user_state_summary` — **aggregated across users**, one value shared by all users of a block. **Correction (architecture review):** an earlier draft called this "the anonymous 'who's stuck' signal, by construction." That over-claimed. The scope does give aggregation for free, but the field is **writable from any student's request**, so a counter kept there is manipulable by students and cannot be the sole basis of an instructor-facing signal. Where struggle signals are used at all, we derive them from the Completion/Grades APIs plus **server-side** aggregation, with a k=5 suppression floor. *(That feature is deferred from the first version — design doc §1.2 — so in the MVP this scope is simply unused.)*
  - `Scope.preferences` — one user, per block-type → per-student toggles (e.g., Socratic mode).
- **In what form.** Python objects with typed fields; serialized to/from **OLX**.
- **How it hands off.** The runtime renders `student_view` (HTML Fragment + JS) and routes AJAX to `@XBlock.json_handler` methods. **`XBlockAside`** renders alongside every block it applies to, with its own scoped fields and a reference to the wrapped block. **Note the precision:** *every block*, not every lesson — so an aside must filter with `should_apply_to_block()` (the runtime also filters via `get_applicable_aside_types()`), or a unit with eight blocks renders eight tutors. Ours binds at `vertical` level only.

### 4.3 Content storage layer (Modulestore / Contentstore — current model)

- **What it is.** The code that reads/writes course content (modulestore; today **Split Mongo**) and course files (contentstore).
- **What data / where / form.**
  - **Modulestore (Split Mongo, current):** structure/settings in a `structures` collection, content-scoped values in a `definitions` collection. **Documents are immutable and versioned**; a course keeps **draft** and **published** branches. *(The platform is evolving toward Learning Core's Django-ORM model; we assert no timeline — see §1.)*
  - **Contentstore (GridFS):** binary assets (PDF, WAV, JPG, video) chunked to exceed 16 MB; assets can be **locked** (enrolled-only) or **unlocked**.
- **How it hands off.** Read through the platform runtime in-process; the whole course exports as an **OLX `.tar.gz`** (structure + `/static/`), which is our clean offline copy for development.
- **Constructor note — the access-path tradeoff (revised, source-verified).** Reading content **directly from the modulestore is an implementation choice that couples us to platform internals**, not the sanctioned default. The concrete API (verified in `xmodule/modulestore/`): `modulestore()` returns the **Mixed** store, and `get_course(course_key)` / `get_item(usage_key)` / `get_items(course_id, ...)` read blocks in-process. Crucially, the **published-branch guarantee is a real, first-class mechanism**, not a convention: the store exposes a `branch_setting(ModuleStoreEnum.Branch.published_only)` context manager (and a `RevisionOption` enum with `published_only` / `draft_only` / `draft_preferred` / `all`), so ingestion runs its reads *inside* a `published_only` context and **draft content is structurally unreadable during ingestion**. The coupling risk is specific: `modulestore()`/Mixed is the *current* Split-Mongo-era API and sits in the layer the platform is evolving away from — so we isolate it behind our own thin adapter (`content_adapter.py`, exposing `get_block` / `iter_leaves` / `get_course_meta`) so any future storage change touches one module, not the whole ingestion pipeline. We commit to the adapter, not to a prediction about what replaces the store or when.
- **Open item (unverified — test on a running instance).** Two sub-assumptions are *not* yet confirmed: (a) exactly what `get_item` returns for a given block type — clean text vs. a descriptor that must be rendered — and (b) whether reading from a **background event consumer** (which runs outside the normal Django request context) behaves like reading inside a live request, since `branch_setting` partly derives the branch from the current request. A consumer likely must set `published_only` explicitly rather than inherit it. Both belong on the "verify on Tutor before relying on them" list.
- **Constructor note — the Blocks-API scope (revised).** The public REST Blocks API returns structure and completion well, but **it is not designed to expose every internal content representation required for AI ingestion** (e.g., full raw lesson bodies for arbitrary block types). That's a scope boundary of the API, not a defect — so we source lesson text via the runtime/OLX, and use the API for what it's built for.

### 4.4 `openedx-events` + the Event Bus (the coordination layer)

- **What it is.** The Hooks Extension Framework's event system — Open edX-specific Django signals, broadcastable across services via a pub/sub event bus.
- **What data / form (verified against `openedx-events` source).** `XBLOCK_PUBLISHED`'s payload is `XBlockData`, which carries exactly three fields: **`usage_key`** (block identifier — what our delete-then-insert keys on), **`block_type`** (e.g. `chapter`, `html`, `problem`), and an optional **`version`** (a UsageKey with branch+version data, usable for our dedup "already ingested this version?" check). Event type string: `org.openedx.content_authoring.xblock.published.v1`, keyed on `xblock_info.usage_key`. Avro-serialized for the bus.
- **Critical firing behavior (verified — changes the pipeline).** A publish fires **one event for the published *container*, not one per changed child.** Per the signal's own docstring: publishing a section with changes in multiple units fires a *single* event with the section's details (`usage_key="section-key", block_type="chapter"`) — **not** events for the individual units, and **not** the leaf `html`/`problem` blocks that hold the text we embed. **Design consequence:** on each event, ingestion must **resolve the event's `usage_key` down to its descendant leaf blocks** (`get_item(usage_key, depth=...)` then walk children, or `get_items` scoped to that subtree) and re-ingest those leaves. Skipping this step means a section-level publish silently misses the actual lesson content. This resolution step is now explicit in the pipeline (5.2).
- **Events we use.** `XBLOCK_PUBLISHED`, `XBLOCK_DELETED`, `XBLOCK_DUPLICATED` (authoring); enrollment and grade/completion events (learning).
- **How it hands off / why it's safe.** A consumer registers `@receiver(XBLOCK_PUBLISHED)` and runs `consume_events`. This is how `edx-exams` coordinates with the LMS without direct calls. Crucially, **a publish event fires only on publish** — draft edits emit nothing — which is exactly the property Principle 3 relies on.

### 4.5 REST APIs (Course Blocks, Enrollment, Grades/Completion)

- **What it is.** Authenticated (JWT/OAuth) endpoints for reading structure and learner data from outside the LMS process.
- **What data / form.** Course Blocks (`/api/courses/v1/blocks/`) returns the user-scoped block tree with completion; Enrollment/Grades/Completion return who's enrolled, grades, and per-block completion (the "who's stuck" raw signal). JSON over HTTP; `openedx-rest-api-client` handles auth.
- **How it hands off.** Used for **progress/enrollment**; lesson text comes from the runtime/OLX (per 4.3).

### 4.6 `openedx/frontend-platform` (the frontend foundation)

- **What it is.** The shared base every MFE builds on: auth, API-client/JWT handling, logging, monitoring, i18n, config.
- **What data / form.** No course data; it manages the authenticated session/JWT the browser uses and environment config.
- **How it hands off.** Gives each MFE an authenticated API client and user identity, so MFEs call LMS APIs as the logged-in user.

### 4.7 `openedx/frontend-app-learning` (the learner experience MFE)

- **What it is.** The React app rendering the course experience (courseware, navigation, progress).
- **What data / form.** Pulls structure and progress from LMS APIs; **renders each XBlock in a sandboxed iframe**.
- **How it hands off / the constraint.** The iframe sandbox scopes a block to its own unit. So our tutor-as-XBlock is **per-unit** (fits "ask about this lesson"); a course-wide floating tutor is a **Frontend Plugin Slot**, not an XBlock (see §3).

### 4.8 `openedx/paragon` (the design system)

- **What it is.** The official React component library + **design tokens** (theming).
- **What data / form.** No data — UI components and style tokens.
- **How it hands off / the constraint.** MFEs use Paragon React; **XBlocks are not React**. Our tutor block matches Paragon's look with plain HTML/CSS rather than embedding Paragon React.

### 4.9 Frontend Plugin Slots (UI extension points)

- **What it is.** Named "slots" in MFEs where custom UI is injected without forking the frontend — the sanctioned course-wide UI path.
- **How it hands off / maturity caveat.** The correct home for the floating tutor, but the slot system is **still maturing** (few slots exposed, tooling not fully seamless) — the reason it's Phase 2 (see §3).

### 4.10 `openedx/openedx-proposals` (OEPs — the rulebook)

- **What we take from it.** **OEP-52** (event bus — validates our ingestion trigger), **OEP-45** (Tutor is the official dev/deploy tool), **OEP-2** (`openedx.yaml` repo metadata) plus Apache-2.0 licensing and the standard layout — the standards our contribution follows. **OEP-65** (frontend composability) governs MFE module-federation and shared-dependency versioning — relevant background for building in the MFE ecosystem, but *not* itself the UI-injection mechanism; the floating-UI path is the **Frontend Plugin Slots** system (see §3 and 4.9), which is separate and still maturing.

### 4.11 Tutor (run & deploy)

- **What it is.** The official Docker way to install/run/develop Open edX locally.
- **How it hands off.** Installing our XBlock is one line (`OPENEDX_EXTRA_PIP_REQUIREMENTS`) plus enabling it in the Advanced Module List. We develop block logic in the lightweight **XBlock SDK workbench** and use full Tutor for integration testing.

---

## 5. End-to-end workflows

### 5.1 Content lifecycle (author → **draft** → publish → AI-ready)

```
Instructor in STUDIO
   ├── types lesson / quiz     → Modulestore  (DRAFT branch)
   └── uploads PDF / slides     → Contentstore / GridFS  (DRAFT)
              │
              ▼
        ┌───────────────┐   content may stay in DRAFT indefinitely
        │  DRAFT state  │   → NO event fires → AI ingests NOTHING
        └──────┬────────┘
              │ instructor clicks PUBLISH
              ▼
   Platform emits  XBLOCK_PUBLISHED  ─▶ thin receiver ─▶ CELERY ─▶ INGEST WORKER
```
**The draft state is explicit and deliberate:** ingestion triggers **only** on publish. This is a *safety property*, not just a technical detail — the tutor can never teach from unreviewed draft content (Principle 3).

**Two corrections to the naive reading of this diagram** (both verified; see the design doc §5.1–5.2):

1. **Publish events alone are not enough to populate the index.** A course published *before* our plugin was installed fires no event, ever — so an event-only design leaves every pre-existing course invisible to the tutor. A **one-time bootstrap command** walks the published tree and indexes it; events are the *incremental* path on top of that. This is precisely how the platform populates its own Meilisearch index (`reindex_studio`, one-time then automatic).
2. **The receiver must not do the work.** `openedx-events` signals are Django signals, so a `@receiver` runs synchronously inside the publishing request — which is a **Studio** request, since `XBLOCK_PUBLISHED` is a `content_authoring` event. Doing extraction and embedding inline would make the instructor's *Publish* button wait on a third-party embedding API, and fail when that API fails. The receiver therefore validates and **enqueues a Celery task**, then returns. Again this mirrors the platform's own indexer, which pairs `handlers.py` signal handlers with `tasks.py` Celery tasks.

### 5.2 Ingestion pipeline (with validation, dedup, versioning, metadata)

```
XBLOCK_PUBLISHED (carries usage_key + block_type + version)
      │
   RESOLVE TO LEAVES  → event points at the PUBLISHED CONTAINER (e.g. a section),
      │                  not the changed leaves. Walk usage_key's descendants
      │                  (get_item depth=... / get_items) to the html/problem
      │                  blocks that actually hold text. Ingest THOSE.
      │
   VALIDATION      → is each leaf authorized, supported content? else skip
      │
   DEDUPLICATION   → have we already ingested this usage_key@version? if yes, skip
      │
   EXTRACT text   → (OCR / transcript / table extraction: best-effort; see risks)
      │
   CHUNK
      │
   EMBED
      │
   WRITE-THEN-SWAP  → write new chunks under usage_key@new_version (old untouched)
      │              → verify → flip the ACTIVE pointer (atomic) → GC old versions
      │              NEVER delete first: a failure between delete and write would
      │              erase a live lesson from the index permanently
      │
   WRITE:  Vector DB  +  paired METADATA store
              metadata per chunk:
                identity   → tenant, course_id, offering_id, usage_key,
                             block_id, block_type
                versioning → version, course_version, publish_time, updated_at
                nature     → content_type (lesson|problem|transcript|slide|
                             past_paper|clo_doc|student_note), language
                targeting  → CLO, week, topic
                isolation  → student (personal uploads only)
```
**Three rules stated explicitly.** (1) **Resolve parent → leaves:** a publish event names the container that was published, not the individual changed blocks, so we must walk down to the leaf blocks before ingesting — otherwise a section-level publish misses the lesson text entirely. (2) **Re-publish = write-then-swap, keyed on `usage_key@version`** — never append, and *never delete first*. Retrieval filters on the active version, so an updated `Lecture5` leaves no stale chunks behind; but because the old version is only removed *after* the new one is verified, a mid-pipeline failure leaves the previous good state intact instead of erasing a live lesson from the index. (3) **Every step is retryable and every failure is recorded** — permanent failures land in a `failed_ingestions` table surfaced by the reconciliation sweep, so a gap is detectable rather than silent. The **metadata store is a named, first-class component**, not an afterthought: it's what lets retrieval filter by tenant, student, version, and CLO.

**Beyond publish — the rest of the lifecycle.** `XBLOCK_DELETED` and `XBLOCK_DUPLICATED` exist and are handled; **`COURSE_IMPORT_COMPLETED`** and **`COURSE_RERUN_COMPLETED`** exist and are the right trigger for a *single budgeted bulk index* rather than absorbing a flood of per-block events. **There is no unpublish event** (verified against the `openedx-events` reference) — so unpublished content would otherwise remain citable indefinitely. Mitigation is a **periodic reconciliation sweep** that compares the index against a fresh `published_only` tree walk and drops orphans; the residual window is stated rather than claimed closed.

### 5.3 Student query (grounded + permission-safe)

```
Student asks "Explain deadlock"
   → Permission check (enrollment; JWT scope)
   → Retrieve ONLY: this course's content  +  this student's own uploads namespace
   → RAG: relevant chunks → LLM (via LiteLLM router)
   → Answer + citation to source block   (Socratic mode optional)
```

### 5.4 Final Exam Prep (flagship second feature)

```
"Prepare me for finals"
   → Collect: CLOs + slides + past papers (student's private namespace)
             + official course content + grades/weak topics
   → Planner agent → study plan organized by CLO
   → Generate CLO-tagged practice from past-paper patterns
   → Track mastery per CLO → target the weak ones
```

### 5.5 Instructor "AI improves the course" (safe write path — Principle 2 in action)

*Designed; deferred from the MVP — see the design doc §1.2. The platform hazard below is why it is specified in full anyway.*

```
"Concept X: 60% stuck"  (completion + server-side aggregation, k ≥ 5 students)
   → AI drafts a fix (extra explanation / new practice item)
   → PROPOSAL QUEUE  ← our storage, OUTSIDE the course tree
   → instructor reviews  ←  human approves
   → ACCEPT
        ├── does this unit hold the instructor's OWN draft work?
        │      no  → write to draft → publish        (→ triggers 5.1 re-ingestion)
        └──    yes → show exactly what else would go live, then:
                     publish-only-this / publish-all / cancel
```
**Why the AI does *not* write into the course's draft branch (corrected — this is a real platform behaviour, not a preference).** Open edX publish is **subtree semantics**: publishing a node publishes its children, and children removed in the source are removed at the destination. So AI content parked in a unit's draft would go live the moment the instructor published *anything else* in that unit — a typo fix would silently ship unreviewed AI content. Parking in draft is therefore not an approval gate at all; it just delays exposure until an unrelated action triggers it.

Keeping proposals **outside the course tree** inverts the flow so that **the human's accept is what causes the write**. There is then no state in which unreviewed AI content can be published by accident, and Principle 2 is enforced by where the data lives rather than by whether an instructor happens to notice a new block.

**And the same hazard applies in reverse at the moment of accepting.** Subtree semantics do not switch off just because a human clicked approve: if the target unit holds the instructor's *own* unfinished draft edits, a naive "write to draft and publish" would ship those too. Hence the conflict check above. The invariant to hold onto is symmetrical — **no publish caused by CourseMate ever makes content live that the instructor has not seen in that moment**, in either direction.

### 5.6 Upload storage architecture (previously underspecified — now explicit)

*Applies to **both** instructor Exam Prep Packs and student personal uploads; neither is written into the Contentstore (design doc §7.7). The **self-serve upload UIs are deferred** from the MVP — the pipeline below is real and is driven by a load command against a pre-prepared pack. What's missing is the button, not the machinery.*

```
Student → Upload PDF (past paper / CLO doc / notes)
        → AI Upload Service (our service, NOT Open edX)
        → OBJECT STORAGE (S3 / Azure Blob / MinIO)   ← raw file lives here
        → OCR / parse → chunk → embed
        → PRIVATE per-student Vector namespace  +  metadata (student, tenant, CLO)
```
**Why this is *not* stored inside Open edX (pre-empting the obvious question):** these are **personal, non-course artifacts** that a student owns — they are outside the platform's course-data model and shouldn't live in the course's modulestore/contentstore. Storing them in our own object storage keeps them private to the student, keeps the platform's course data clean, and lets us apply per-student isolation that the course data model isn't designed for.

### 5.7 Isolation — and an honest correction about tenancy

```
MVP (built):  ONE Open edX deployment = ONE tenant
   └── course content namespace
         └── per-STUDENT private namespace (their uploads)   ← the boundary that
                                                                matters right now

FUTURE (designed, deferred):  central service, many institutions
   ├── University A → tenant namespace
   ├── University B → tenant namespace
   └── Company C    → tenant namespace
```

**The correction:** earlier drafts drew the multi-tenant picture *and* specified in-process `modulestore()` reads inside the LMS. Those are mutually exclusive — each institution runs its own Open edX deployment, and you cannot read another organisation's modulestore across the internet. So the MVP is a **per-instance plugin with a single tenant**.

`tenant` stays in the metadata schema and in cache keys anyway, holding one constant value, because retrofitting an isolation key later is expensive and carrying it now is free.

**Multi-tenant SaaS is deferred for architectural reasons, not effort:** it needs a different content-access path (no in-process reads), cross-instance authentication, per-institution data-residency answers, and per-tenant key management. Those are open design questions.

**Per-student isolation is *not* deferred** — it is live from day one, enforced by metadata filters applied *before* ranking (so unauthorized content is never even a candidate), by per-student namespaces, and by a response cache that refuses to store or serve anything whose retrieval touched a personal namespace.

---

## 6. Data map (where each thing lives, in what form, how we read it)

| Data | Owner / store | Form | How our AI gets it |
|------|---------------|------|--------------------|
| Lesson text, problems | Modulestore (current model; platform evolving toward Learning Core), published branch | Versioned XBlock JSON / OLX | Platform runtime in-process via our content adapter, or OLX export |
| PDFs, slides, images, video | Contentstore / GridFS | Binary blobs | Asset path / OLX `/static/` |
| Video transcripts | Contentstore (as assets) | Text (SRT/VTT) | Read as asset; text is the useful part |
| Enrollment, grades, completion | MySQL | Relational rows | Enrollment / Grades / Completion REST APIs |
| "Content changed" trigger | openedx-events / event bus | Avro `XBlockData` (`usage_key`, `block_type`, `version`) — fires for the published *container*, resolve to leaves | `@receiver(XBLOCK_PUBLISHED)` |
| Aggregate "who's stuck" *(deferred from the MVP)* | Completion/Grades APIs + **our server-side aggregation** | Aggregated counts | Anonymized, **suppressed below k=5 students**. *Not* `user_state_summary` alone — that scope is student-writable, so a counter kept there is manipulable and unreliable as an instructor-facing signal |
| **Student uploads (CLOs, past papers)** | **Our object storage (S3/Azure/MinIO), NOT Open edX** | Loose PDFs → chunks | Our upload service → OCR/parse → **private** vector namespace |
| **Retrieval metadata** | **Paired metadata store** | tenant, course_id, offering_id, usage_key, block_id, block_type, version, **course_version, publish_time, updated_at, content_type, language**, student, CLO, week, topic | Written alongside every embedding; filtered *before* ranking |

---

## 7. Constructor's verdict (what's solid, what's the risk)

**Solid, works as designed:** ingestion triggered on **publish only** (on top of a one-time bootstrap); the Scope-based privacy model (`user_state` for private chat memory); the **proposal-queue** approval path (Principle 2); per-student isolation; and a named metadata store with **write-then-swap** versioning. These ride native platform behaviours.

**Corrections this analysis originally got wrong** (found in architecture review, verified against source — see `Architecture_Review_Round2.md`):
- **"Event-driven ingestion" is not sufficient on its own.** Content published before install fires no event, so an event-only design leaves existing courses invisible. A bootstrap command is mandatory, not optional (§5.1).
- **"Draft-only, human-publishes" was not a safety path.** Open edX publish is subtree semantics, so AI content parked in draft ships the next time the instructor publishes anything in that unit. Replaced by the proposal queue (§5.5).
- **"Delete-then-insert" could destroy a live lesson's index entry** if the pipeline failed mid-way. Replaced by write-then-swap (§5.2).
- **`user_state_summary` is student-writable**, so it cannot be the sole source of an instructor-facing signal (§6).

**Real risks, named honestly:**
1. **Storage is evolving** (the platform is moving toward Learning Core, on a timeline we do not predict), so we depend on stable APIs/events behind an adapter, not on DB internals.
2. **Direct modulestore access couples us to internals** — used only where no service path exists, and flagged for the Learning Core transition.
3. The **Blocks API is out of scope** for full lesson-text ingestion by design — we source text via runtime/OLX.
4. **There is no unpublish event.** Unpublished content remains citable until the next reconciliation sweep — a real window we state rather than claim to have closed (§5.2).
5. **OCR / formula / table extraction** degrades on scanned/handwritten material — clean PDFs and transcripts are reliable, the rest is best-effort.
6. **CLO mapping** is AI-*assisted*, not automatic truth.
7. The **floating course-wide tutor** depends on the **still-maturing** Frontend Plugin Slot system — hence Phase 2.
8. **Course import/rerun event behaviour is unverified** — handled either way via `COURSE_IMPORT_COMPLETED`/`COURSE_RERUN_COMPLETED` plus a per-course cost ceiling, but the actual firing pattern determines which path runs.

**One-line summary:** Open edX stays the source of truth for content, permissions, and progress; our AI extends it through a **thin** XBlock and an **asynchronous** ingest worker (never modifying the core, never blocking a platform action), learns only from published/authorized content via a bootstrap index plus publish events, writes new content only to a **proposal queue outside the course tree** that a human must accept, and stores personal student uploads in its own isolated object storage + vector index — with all content access behind an adapter so it stays compatible with the platform's evolution toward Learning Core, whatever shape and schedule that takes.
