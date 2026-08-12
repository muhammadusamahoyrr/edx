# CourseMate — Repository & Folder Structure Plan

*Derived from `CourseMate_Complete_Design.md` (v6). Per §17, that document wins on any conflict. This one answers a single question: **what does the repository look like, and why is each boundary there?***

**The rule this document is written to.** A folder structure is not organisation, it is **enforcement**. The design makes five hard promises — the LMS never does AI work (§3.4), one module reads the modulestore (§3.3), one interface reaches the knowledge layer (§6.5), evaluation never sits in the request path (§4), AI content never touches the course tree (§9.1). Each of those is either a directory boundary that CI can check, or it is a promise that decays the first busy week. Every top-level decision below traces to one of them.

---

## 0. The three decisions that determine everything else

### 0.1 One repository, three installable packages

**Decision: a monorepo with separately-installable packages, not a single package and not three repos.**

Two artifacts ship to two different places on two different schedules: an Open edX plugin baked into the Tutor image, and a service container. They cannot be one package — the LMS image would then pull `langgraph`, `litellm`, and a cross-encoder into a gunicorn worker, which is precisely the coupling §3.4 exists to prevent. They should not be three repos either: one engineer, 3.5 weeks, and a wire contract that changes weekly. Split repos buy independent versioning nobody needs yet and cost a cross-repo PR for every contract change.

| Package | Ships to | Depends on |
|---|---|---|
| `coursemate-platform` | Open edX image (LMS + CMS + Celery) | `xblock`, `openedx-events`, `PyJWT`, `httpx`, contracts |
| `coursemate-service` | Its own container | `fastapi`, `langgraph`, `litellm`, vector client, reranker, contracts |
| `coursemate-contracts` | Both, as a dependency | `pydantic` only |

**The contracts package is the load-bearing one.** §3.4 defines three network hops (student traffic, invalidation, ingest writes) and §7.6 defines a question record that both sides read. Without a shared schema module the wire format is duplicated in two languages of the same language, and drift shows up as a runtime 422 in week 3. It stays dependency-free so importing it into an LMS process costs nothing.

**Promotion trigger, written down now:** split into separate repos when the service acquires a second consumer (§6.5's MCP trigger) or a second customer deployment on a different platform version.

### 0.2 Where ingestion work happens — a design inconsistency, resolved

Three sections currently disagree:

- §3.4's topology box puts `extract · chunk · embed · write` **in the in-platform Celery worker**.
- §4's layer table lists Ingestion as running in the **CourseMate service, background**.
- §6.4 puts the **embedding cache** in the service, alongside the vector store.

All three cannot hold. If the worker embeds, then either the embedding cache lives in the platform (contradicting §6.4) or every worker embed is a cache miss against a cache it cannot reach.

> **Resolution, and it is the one the folder structure assumes: the platform worker *reads*, the service *transforms*.**
>
> **Platform worker:** resolve container → leaves, validate, dedup on `usage_key@version`, extract text via `content_adapter`, `POST /ingest/blocks` with the extracted text + metadata.
> **Service:** chunk (§5.5), embed (through the embedding cache), write → verify → swap → GC (§5.3).

Four reasons, in order of weight:

1. **The embedding cache and the embedder end up co-located**, which is the only arrangement in which §6.4's cache tier does anything.
2. **Write-then-swap becomes local.** §5.3's four steps are one transaction against one store. Split across a network hop, "verify then swap" needs a distributed protocol to survive a worker dying between steps 2 and 3.
3. **Model credentials stay out of the Open edX deployment**, which is the same instinct as §10.4 — the platform holds no provider keys at all, for embeddings or generation.
4. **Layer 1 gets thinner**, which §4 states as an explicit goal. The platform package's entire dependency footprint becomes: XBlock, events, an HTTP client, JWT.

What this costs: the extracted text of a course crosses the network once per ingest instead of only its vectors. For a 500-block course that is ~2 MB over a private network, on a background path with no latency budget. That is not a real cost.

**Chunking therefore lives service-side.** §5.5's first rule — *block boundaries are authoritative, never merge two blocks* — is preserved by the payload shape: the worker sends one record **per leaf block**, so the service is structurally incapable of merging across blocks. The invariant moves from a coding convention into the wire format, which is stronger.

*Action: fold this resolution back into §3.4, §4 and §6.4 of the design document, per its own maintenance rule (§17).*

### 0.3 How "designed but not built" is represented

§1.2 defers eleven subsystems and insists they are *"designed and dormant, not hand-waved."* A folder structure has to express that without lying in either direction. Three conventions:

| Convention | Applies to | Example |
|---|---|---|
| **Real module, schema only, nothing imports it** | Deferred subsystems whose *data shape* is the design contribution | `service/proposals/` — the queue record with `origin`, no accept logic, no route |
| **Real module behind a feature flag, off by default** | Deferred work sitting on a seam that already exists | `service/knowledge/lexical/` (Meilisearch half, §6.1), `platform/xblock/aside.py` (§3.1) |
| **Not created at all** | Deferred work needing a surface that does not exist | Upload UIs (§7.2), review UI (§9.2 #4), notifications |

**Every dormant directory carries a `README.md` with one line: what it is, which design § specifies it, and why it is not wired.** A dormant folder without that note reads as abandoned code within a month, and the next reader deletes it or, worse, half-implements it.

**No `future/`, `wip/`, or `v2/` directories.** Deferred code sits in its real home or does not exist. A parking-lot folder is where scope goes to become invisible — which is the exact failure §1.2 was written to prevent.

---

## 1. Top level

```
coursemate/
├── packages/
│   ├── coursemate-contracts/      # wire schemas — both sides import, zero heavy deps
│   ├── coursemate-platform/       # Open edX plugin: XBlock + receivers + ingest worker
│   └── coursemate-service/        # the CourseMate container: knowledge, boundary, agents
├── eval/                          # LAYER 5 — offline, own deps, never in the runtime image
├── deploy/                        # Tutor plugin, ingress route, compose, env templates
├── tools/                         # week-1 verification, ops scripts, fixtures
├── docs/                          # the design set (§17) + ADRs + archive
├── tests/                         # cross-package contract tests only
├── .importlinter                  # architecture rules, enforced in CI
├── Makefile
└── README.md
```

Seven top-level entries, and each one answers a question a newcomer actually asks: *what runs where* (`packages/`), *how do I know it's good* (`eval/`), *how do I run it* (`deploy/`), *how do I check the platform assumptions* (`tools/`), *why is it like this* (`docs/`).

**`eval/` is top-level, not inside the service, and that placement is the point.** §4 says layer 5 *"sits beside the pipeline, not at the end of it."* Ragas, the datasets and the rubric forms have no business in the container that answers a student's question. Top-level with its own dependency file makes "never in the request path" a packaging fact rather than a discipline.

---

## 2. `packages/coursemate-platform` — the only code inside the customer's platform

Installed into the LMS, CMS and Celery images. §4: *"the only part inside the customer's platform, and it is deliberately the thinnest."* If this tree grows, something has gone wrong.

```
coursemate-platform/
├── pyproject.toml               # entry points: openedx.djangoapp (lms+cms), xblock.v1
├── coursemate_platform/
│   ├── apps.py                  # plugin_app: settings + urls config for BOTH lms and cms (§3.4 r4)
│   ├── settings/
│   │   ├── common.py            # service URL, JWT signing key ref, timeouts, flags
│   │   └── production.py
│   │
│   ├── adapters/
│   │   └── content_adapter.py   # §3.3 — THE ONLY MODULE THAT MAY IMPORT modulestore/xmodule
│   │                            #   get_block · iter_leaves · get_course_meta
│   │                            #   OWNS branch_setting(published_only) internally — see V-C
│   │
│   ├── events/
│   │   ├── cms_receivers.py     # XBLOCK_PUBLISHED / _DELETED / _DUPLICATED  → validate + enqueue
│   │   │                        #   IMPORT/RERUN handlers: DEFERRED (§5.4)
│   │   │                        #   XBLOCK_CREATED/_UPDATED: deliberately NOT subscribed (V-B)
│   │   └── lms_receivers.py     # COURSE_UNENROLLMENT_COMPLETED → post invalidation (§3.4 r4)
│   │
│   ├── tasks/
│   │   ├── ingest.py            # resolve-to-leaves → validate → dedup → extract → POST (§5.2)
│   │   ├── bootstrap.py         # full-course walk (§5.1), idempotent, --incremental semantics
│   │   └── reconcile.py         # nightly sweep + failed_ingestions retry (§5.4)
│   │
│   ├── client/                  # SERVER-TO-SERVER ONLY — student traffic never passes through here
│   │   ├── jwt.py               # mint short-lived JWT: user_id, course_id, roles, exp (§3.4)
│   │   ├── http.py              # hard timeout · circuit breaker · retry policy (ingest/invalidation)
│   │   └── endpoints.py         # typed calls, params and returns from contracts
│   │
│   ├── xblock/
│   │   ├── tutor_block.py       # student_view · studio_view · json_handler
│   │   │                        #   MINT A JWT + PERSIST A TURN. Never relays an answer (§3.4 r3)
│   │   ├── aside.py             # DEFERRED (§3.1) — should_apply_to_block → vertical only
│   │   ├── static/{js/src,css,html}/
│   │   └── templates/
│   │       ├── student_view.html
│   │       └── studio_view.html # config + "Index this course" + last-indexed + block count (§5.1)
│   │
│   ├── management/commands/
│   │   └── coursemate_reindex.py   # THIN: validate args → enqueue tasks.bootstrap → report (V-A)
│   │                               #   --course <key> | --all   (no --incremental: it IS the default)
│   │
│   ├── models.py                # failed_ingestions · course_index_state · bootstrap_locks
│   │                            #   course_index_state is RESUMABLE progress, not just a timestamp (V-A)
│   ├── locks.py                 # in-flight lock + cooldown for the query-time backstop (§5.1)
│   └── migrations/
└── tests/
    ├── unit/                    # runs anywhere — client, locks, JWT, task logic with a faked adapter
    └── platform/                # needs a running Open edX — receivers, adapter, command
```

**Five things this layout is doing deliberately:**

1. **`content_adapter.py` is a single file, not a package.** §3.3's promise — *"if the store changes, that module changes; nothing else does"* — is checkable at a glance only while it is one file. The moment it becomes `adapters/split_mongo/`, `adapters/learning_core/`, the claim needs a diagram to verify. If a second backend genuinely lands, that is the moment to split it, and not before.

   **And it owns the published-branch context internally — it does not ask callers to wrap.** Verified from source (V-C): `branch_setting` is thread-local with a default of `None`, so a Celery worker inherits nothing and falls back to the store's own default. An API shaped as *"call `iter_leaves` inside a `branch_setting` block"* means one forgotten `with` silently indexes draft content — a Principle 3 violation with no error, no test failure, and no symptom until a student is cited unpublished text. `iter_leaves()` opens the context itself. There is no adapter function that reads content outside it.

2. **Receivers are split by *process*, not by event type.** §3.4 rule 4 was a corrected bug — an earlier draft put all receivers in the CMS and lost enrollment events. `cms_receivers.py` / `lms_receivers.py` makes the process split the first thing you see, so the same mistake cannot be made silently. `apps.py` registers each to the right `plugin_app` key.

3. **`tasks/` holds three files matching §5.1/§5.2/§5.4** — incremental, bootstrap, reconcile. These are the three ingestion triggers and they should be three obvious files, because "which of these ran?" is the first debugging question when the index is wrong.

4. **The XBlock's discipline is a directory fact.** `xblock/` contains no retrieval, no model call, no embedding — and `.importlinter` forbids `coursemate_platform` from importing anything AI-shaped. §3.4's rule 3 stops being a comment.

   Since design v8 the block does not relay answers either: `json_handler` mints a JWT and persists completed turns, and the **browser** streams from the service on a same-origin path. The streaming client is therefore JavaScript in `xblock/static/js/src/`, not Python in `client/` — `client/` remains server-to-server only (ingest POSTs, invalidation), which is why its circuit breaker guards background work rather than student traffic.

5. **Two test directories, because they cost differently.** `unit/` runs in a plain venv in seconds; `platform/` needs Tutor and runs rarely. Mixing them means the fast suite is never fast, so nobody runs it.

**Dependency ceiling for this package, worth stating as a rule:** `xblock`, `httpx`, `PyJWT`, `pydantic`, `coursemate-contracts`. Anything else needs an ADR. This is the single most valuable constraint in the repository — it is what makes "CourseMate cannot degrade the platform" (Principle 8) structurally true rather than aspirational.

---

## 3. `packages/coursemate-service` — layers 2–4

```
coursemate-service/
├── pyproject.toml
├── coursemate_service/
│   ├── main.py
│   ├── config.py                    # tenant constant (§3.5), τ, budgets, flags
│   │
│   ├── api/                         # THREE credential classes, kept apart on purpose (§3.4)
│   │   ├── deps.py                  # verify JWT → re-derive authz → rate limit (§10.1, §10.8)
│   │   ├── chat.py                  # SSE to the BROWSER          ← student JWT
│   │   │                            #   rate limit · timeout · circuit breaker live HERE,
│   │   │                            #   not in the XBlock (§3.4 r3, design v8)
│   │   ├── examprep.py              # student path                ← student JWT
│   │   ├── ingest.py                # chunk·embed·write·swap      ← service credential
│   │   ├── invalidation.py          # cache scope notices          ← service credential
│   │   └── admin.py                 # retirement / deletion (§10.7)
│   │
│   ├── boundary/                    # §6.5 — the security chokepoint
│   │   ├── interface.py             # CourseIntelligence: the 4 tools, all keyed on offering_id
│   │   ├── impl.py                  # identity → authz → filter → knowledge → audit, in that order
│   │   ├── authz.py                 # enrollment/role re-check per call, never per session (§10.1)
│   │   ├── filters.py               # tenant/student filter built BEFORE ranking (§6.3, §10.2)
│   │   └── audit.py
│   │
│   ├── knowledge/                   # LAYER 3
│   │   ├── vector_store/
│   │   │   ├── client.py
│   │   │   └── swap.py              # write → verify → swap pointer → GC (§5.3)
│   │   ├── metadata/
│   │   │   ├── schema.py            # §6.2 — identity·versioning·nature·targeting·isolation
│   │   │   ├── questions.py         # §7.6 question records
│   │   │   └── clo.py
│   │   ├── cache/
│   │   │   ├── embedding.py         # content-addressed, never stale
│   │   │   ├── response.py          # key INCLUDES effective permission scope (§6.4)
│   │   │   ├── metadata.py          # short TTL — revoked enrollment must stop working fast
│   │   │   └── policy.py            # "personal-namespace results are never cached" lives HERE, once
│   │   └── lexical/                 # DEFERRED (§6.1) — Meilisearch half of hybrid. README.
│   │
│   ├── ingestion/                   # LAYER 2 — runs on publish, never on question
│   │   ├── chunking.py              # §5.5 — semantic boundaries within one block, 512–1024 guard
│   │   ├── embedding.py
│   │   ├── pipeline.py              # orchestrates chunk → embed → swap.write
│   │   └── examprep/                # §7
│   │       ├── router.py            # digital | scanned→VLM | clo_doc→schema extraction (§7.3)
│   │       ├── extractors/
│   │       ├── clo_extractor.py     # schema-guided + confidence, human-confirmed (§7.3)
│   │       ├── question_parser.py   # → §7.6 records: year, exam_type, marks, provenance
│   │       └── clo_tagger.py        # AI-proposed, correctable (§7.5)
│   │
│   ├── agents/                      # LAYER 4 — may NOT import knowledge/ directly
│   │   ├── graph.py                 # shallow supervisor, iteration cap (§8.3)
│   │   ├── nodes/
│   │   │   ├── rewrite.py           # schema-constrained: query + filter, cannot widen scope (§8.1)
│   │   │   ├── retrieve.py          # calls the boundary, not the store
│   │   │   ├── rerank.py            # skip-under-load degradation, logged (§8.2)
│   │   │   ├── tutor.py
│   │   │   ├── socratic.py
│   │   │   ├── planner.py           # marks-budget study plan (§7.6, §13.2)
│   │   │   └── quiz_generator.py
│   │   ├── guards/
│   │   │   ├── confidence.py        # gate 1: abstain below τ BEFORE generating (§8.5)
│   │   │   ├── citation.py          # cannot cite → abstain
│   │   │   ├── verifier.py          # gate 3: parallel with streaming, not after it (§8.5)
│   │   │   └── injection.py         # trust tiers: chat untrusted, docs semi-trusted (§10.6)
│   │   └── prompts/                 # versioned, referenced by eval runs
│   │
│   ├── models/
│   │   ├── router.py                # LiteLLM: fallbacks · content_policy_fallbacks · RetryPolicy
│   │   │                            #   · allowed_fails cooldown (§8.4) — CONFIG, not code
│   │   └── reranker.py              # CPU cross-encoder — the ONE self-hosted model (§8.2)
│   │
│   ├── proposals/                   # DORMANT (§9.1) — schema only, nothing imports it
│   │   ├── schema.py                # proposal_id, offering_id, target usage_key, origin, status
│   │   └── README.md                # "designed, dormant; §9.1; the MVP generates no course content"
│   │
│   └── observability/
│       └── tracing.py               # §11.4 — rewritten query + filters, scores, which provider, τ decision
└── tests/
```

**Why `boundary/` sits between `agents/` and `knowledge/` as its own top-level package:** §6.5's argument is that four things must happen on *every* data access, and scattering them across agent nodes means a new node forgets one. That argument only survives if the boundary is somewhere a new node cannot casually route around. A sibling directory plus an import-linter contract (`agents` must not import `knowledge`) makes forgetting it a CI failure instead of a code-review catch.

**`cache/policy.py` exists as its own file for one reason.** §6.4 and §10.2 both say personal-namespace results are never cached, and call it a security control rather than an optimisation. A rule stated in two design sections and implemented in three cache modules is a rule that will hold in two of them. One module, imported by all three tiers.

**`models/router.py` is configuration.** §8.4 makes a point of it — LiteLLM's fallback and cooldown behaviour is native, *"which is what makes it affordable inside the delivery window."* If that file grows retry logic, the affordability argument has quietly expired.

---

## 4. `packages/coursemate-contracts`

```
coursemate-contracts/
└── coursemate_contracts/
    ├── version.py            # single integer, bumped on any breaking change; both sides assert
    ├── auth.py               # JWT claim set (§3.4)
    ├── chat.py               # request incl. rolling history window (§3.1) · SSE frames · citations
    ├── ingest.py             # ONE RECORD PER LEAF BLOCK — enforces §5.5 rule 1 in the wire format
    ├── invalidation.py       # enrollment/scope notices (§3.4 hop 2)
    ├── examprep.py           # pack + §7.6 question record
    ├── metadata.py           # the §6.2 field set, shared so filters can't drift
    └── errors.py             # incl. the honest-failure codes: abstained, unavailable, preparing
```

`errors.py` deserves its place: §5.1's *"this course is still being prepared"*, §8.5's abstention and §8.4's *"the tutor is unavailable"* are three **different** states that must never be rendered as one generic failure — the entire §5.1 argument is that the difference between *looks broken* and *tells you what's happening* is the difference between a dead demo and a live one. Typed error codes in the shared contract is where that gets enforced.

---

## 5. `eval/`, `tools/`, `deploy/`, `docs/`

```
eval/                              # LAYER 5 — own requirements.txt, never imported by the service
├── datasets/
│   ├── pilot_questions.yaml       # 20–30 q × 4 Ragas metrics (§11.2a)
│   ├── abstention_set.yaml        # covered + uncovered, deliberately mixed (§11.2c) → informs τ
│   └── feature_b_sample.yaml      # 30 generated practice items (§11.3)
├── runners/
│   ├── ragas_run.py               # retrieval AND generation metrics, scored separately (§11.1)
│   ├── abstention_audit.py        # false answers AND false abstentions, both directions
│   └── feature_b_rubric.py        # validity · CLO alignment · provenance · difficulty (§11.3)
├── reports/                       # committed — results with sample size and limitations beside them
└── README.md                      # "single rater in the MVP" stated here, not only in the design doc

tools/
├── verification/                  # §3.6 / Week1_Verification_Plan.md — one script per open item
│   ├── t1_get_item_shapes.py
│   ├── t2_celery_branch_setting.py
│   ├── t3_import_event_behaviour.py
│   ├── t4_meilisearch_embedder.py
│   └── t5_scoped_publish.py
├── fixtures/build_test_course.py  # the deliberately awkward course the plan requires
└── ops/

deploy/
├── tutor-plugin/                  # installs coursemate-platform into LMS/CMS/worker images
│                                  #   AND patches the ingress to route /coursemate/ → service (§3.4 r3)
├── docker-compose.yml             # service + vector store + metadata db + cache
├── env/
└── README.md                      # runbook: bootstrap a course, read a trace, force a sweep

docs/
├── CourseMate_Complete_Design.md      # source of truth (§17)
├── CourseMate_Repository_Structure.md # this file
├── OpenedX_Architecture_Analysis_v2.md
├── Architecture_Review_Round2.md
├── Week1_Verification_Plan.md
├── adr/                               # one file per decision made DURING the build
└── archive/                           # §17 — superseded material, nothing current depends on it
```

**`eval/reports/` is committed to the repository, and that is a product decision.** §1.3 commits to a definition of done *"written in advance"*, and §11 to publishing limitations beside numbers. A results directory in version control means the numbers have a history and cannot be quietly re-run until they look better. It is the cheapest possible implementation of the honesty this design keeps insisting on.

**`tools/verification/` is five scripts because §3.6 lists five open behaviours.** They run in week 1, gate design choices, and write results back into `Week1_Verification_Plan.md`. Keeping them in the repo — rather than in a scratch file, as the plan currently allows — means a result can be re-checked against a new Open edX release instead of re-derived.

**ADRs start when the build starts.** The design document holds decisions made *before* implementation; `docs/adr/` holds the ones made *during* it, where the reasoning would otherwise live only in a commit message. §17's maintenance rule — *"when something moves from built to deferred, search every document for its name"* — is exactly the failure ADRs prevent.

---

## 6. Architectural rules, enforced in CI

`.importlinter` turns five design promises into build failures. This is the highest-leverage file in the repository.

| # | Contract | Design promise it defends |
|---|---|---|
| 1 | Only `adapters.content_adapter` may import `xmodule`/`modulestore`/`opaque_keys` internals | §3.3 — storage change touches one module |
| 2 | `coursemate_platform` may not import `langgraph`, `litellm`, vector clients, or embedding libs | §3.4 — no AI work in an LMS worker; Principle 8 |
| 3 | `agents` may not import `knowledge` — only `boundary` | §6.5 — a chokepoint that cannot be bypassed |
| 4 | Nothing in `api`, `agents` or `knowledge` may import `proposals` | §9.1 — dormant means dormant |
| 5 | Neither package may import `eval` | §4 — evaluation is never in the request path |

Contract 2 is the one that pays for the whole file. It is the promise that most matters commercially — *"CourseMate cannot take your LMS down"* — and the promise most likely to be broken by a small convenient import at 11pm in week 3.

---

## 7. Build order, mapped to the delivery weeks (§1.3)

| Week | Directories that come alive | Gate before moving on |
|---|---|---|
| **1** | `tools/verification/` (all five, first), then `contracts/`, `platform/adapters/`, `platform/events/`, `platform/tasks/ingest.py`, `service/api/ingest.py`, `service/ingestion/`, `service/knowledge/`, `.importlinter` | Publish a block → it appears in the index. Results written back into `Week1_Verification_Plan.md` |
| **2** | `platform/xblock/`, `platform/client/`, `platform/management/`, `service/api/chat.py`, `service/boundary/`, `service/agents/nodes/{rewrite,retrieve,rerank,tutor}` | A student asks in a lesson and gets a cited answer from a bootstrapped course |
| **3** | `service/agents/guards/`, `service/agents/nodes/{socratic,planner,quiz_generator}`, `service/ingestion/examprep/`, `service/models/router.py`, `platform/tasks/reconcile.py`, `eval/`, `deploy/` | Abstains below τ; survives a killed provider; exam-prep pack produces a plan; deployed |
| **4** | `docs/adr/`, `eval/reports/`, a deployment runbook | Numbers published with limitations beside them (§11) |

> **As built (2026-08-12):** `eval/reports/` exists; the runbook was written as
> [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) rather than `deploy/README.md`; `docs/adr/`
> was never created — the decisions and their rejected alternatives live in
> `CourseMate_Complete_Design.md` instead, which is where §17 says they belong.

**The ordering is not arbitrary — it is the design's own risk ordering.** Verification first because §3.6 says an unexpected answer costs nothing in week 1 and a great deal in week 3. Ingestion before the tutor because §5.1 names an empty index as *"the most likely way a demo fails."* `.importlinter` in week 1 because an architectural rule added in week 3 is a rule that finds violations it is too late to fix.

---

## 7b. Source verification, and six corrections

*Every platform claim the structure rests on was re-checked against official sources on 2026-07-30. Thirteen held exactly. Six did not, and four of those change the tree above. Sources listed at the end.*

### Confirmed as stated

| Claim | Source |
|---|---|
| All six lifecycle events exist with those exact `event_type` strings; **no unpublish event; no retirement event** | `openedx_events/{content_authoring,learning}/signals.py` |
| Publishing a parent with changed children fires **one** event carrying the **parent's** details → resolve-to-leaves is mandatory | openedx-events docs, verbatim |
| `content_authoring` fires in **Studio**, `learning` (unenrollment) in the **LMS** → receivers in both | signals.py namespaces + docs |
| Thin receiver → Celery is the platform's own pattern | `content/search/handlers.py` uses `.delay()` |
| `COURSE_IMPORT_COMPLETED`/`RERUN` → **one** bulk task, not per-block | `handlers.py` → `upsert_course_blocks_docs.delay` |
| Meilisearch `index_config.py` has **no** embedder/vector/semantic config; ranking rules as described | `content/search/index_config.py` |
| `should_apply_to_block(cls, block)` is a classmethod; `get_applicable_aside_types` gives runtime-level filtering | `xblock/core.py`, XBlock Runtime API |
| `ModuleStoreEnum.Branch.published_only`; `branch_setting` is a real contextmanager | `xmodule/modulestore/{__init__,mixed}.py` |
| LiteLLM Router: `fallbacks`, `content_policy_fallbacks`, context-window fallbacks, per-error `RetryPolicy`, `allowed_fails` + `cooldown_time` — **config, not code** | LiteLLM routing docs |
| Tutor auto-provisions Meilisearch (Tutor 19+ / Sumac) | Sumac release notes |
| Plugin entry points `lms.djangoapp` / `cms.djangoapp`; `PluginSignals.CONFIG` takes a **separate `RELATIVE_PATH` per project type** | edx-django-utils plugin how-to |
| Retirement is a driver-script **API call**, not an event | tubular / user-retirement docs |

That last one is a quiet win for the tree: `PluginSignals.CONFIG` is keyed by project type, each with its own module path. The `cms_receivers.py` / `lms_receivers.py` split is not a stylistic preference — it is the shape the plugin registry expects.

### V-A — `reindex_studio --incremental` no longer exists *(affects the tree)*

The command CourseMate's bootstrap is modelled on (§5.1) has moved on. `--incremental`, `--experimental`, `--reset` and `--init` are all **removed**: incremental is now the default behaviour, index setup happens in **migrations**, and the command **enqueues a Celery task** (`rebuild_index_incremental`) that *tracks completion state and resumes if interrupted*.

The design copied the Sumac-era flag. Copy the current lesson instead, because it is the more valuable one:

- **Drop `--incremental`.** Idempotent-and-skip-what's-done is the only sensible default; a flag implies the other mode is safe.
- **The command enqueues; the task works.** A management command that embeds 500 blocks in-process dies with the SSH session and resumes from nothing. This is the platform's own scar tissue.
- **`course_index_state` must be resumable progress**, not a `last_indexed_at` display field. It was in the tree as a nicety; it is now load-bearing.

### V-B — the platform's search app does **not** consume `XBLOCK_PUBLISHED` *(no change, but a warning)*

`content/search/handlers.py` subscribes to `XBLOCK_CREATED`, `XBLOCK_UPDATED` and `XBLOCK_DELETED` — because **Studio search indexes drafts**. So "we mirror `content.search`" holds for the *async pattern* and not for the *event choice*. Ours differs correctly: we are published-only per Principle 3.

Two consequences. First, there is **no in-tree consumer of `XBLOCK_PUBLISHED`**, so its firing behaviour is less battle-tested than "verified" implies — which raises, not lowers, the value of the week-1 tests. Second, `XBLOCK_CREATED`/`XBLOCK_UPDATED` **exist and are missing from §5.4's lifecycle table**. They must be listed there as *deliberately ignored, because they fire on draft edits*. An unexplained absence is an invitation: the next person to read that table sees two unhandled events and wires them up, and CourseMate starts indexing drafts.

### V-C — the branch setting is thread-local, so Week-1 Test 2 is already answered *(affects the tree)*

Source: `MixedModuleStore.branch_setting` stores into `self.thread_cache.branch_setting` and restores the previous value on exit; when no context is active the value is `None`.

A Celery worker runs in a different thread with no active context, so it inherits nothing and falls back to the store's own default — which for the draft-capable stores is draft-preferred. **The conclusion is now established from source rather than pending an experiment: pin `published_only` explicitly, always.** §3.6 already said "we pin it explicitly regardless," which was the right instinct; it can now be stated as a verified fact.

Structural consequence, and it is the reason this matters: the adapter must **own** the context rather than expose it. See §2 item 1.

Test 2 still runs in week 1 — 15 minutes to confirm what the fallback actually resolves to on the target release — but it is no longer a gate, and the half-day it was budgeted against comes back.

### V-D — "API keys excluded from course export" has no mechanism *(affects the tree, and simplifies it)*

There is no standard XBlock feature that omits a `Scope.settings` field from OLX export — settings-scoped fields are exactly what OLX serialises. The referenced XBlock is **`open-craft/xblock-ai-evaluation`** (not `openedx/`), and what it actually does is read keys at the XBlock level *falling back to Site Configuration* — i.e. the protection is "put the key somewhere else," not "mark the field non-exportable."

For CourseMate the honest version is stronger than the claim it replaces:

> **The XBlock holds no credentials at all.** It mints a token and renders a UI (§3.4 r3); every provider key lives in the service. The JWT signing key comes from Django settings, never a field. `Scope.settings` carries only non-secret config — enabled, mode, display name.

§10.4 is then satisfied **by construction**, the same move §1.2 makes for Principle 2 — and it deletes a whole category of machinery from the tree. *Action: fix §10.4's second sentence; it currently implies an export-exclusion feature that does not exist.*

### V-E — no target Open edX release is pinned anywhere *(affects the tree)*

The design cites Sumac-era facts. The current stable release is **Ulmo**, with **Verawood** as the June 2026 release. V-A is the proof that this matters: a command the design models itself on changed its entire interface between then and now.

`deploy/tutor-plugin/` must pin a Tutor major version and the platform package must declare a supported release, in `pyproject.toml` and in the README's first paragraph. "Works on Open edX" is not a compatibility statement, and for a plugin it is the first question any operator asks.

### V-F — retirement is a tubular contribution, not just an endpoint *(deferred; note only)*

§10.7's endpoint is necessary but not sufficient. The pipeline is YAML listing state transitions of the form `[RETIRING_X, X_COMPLETE, SERVICE, api_method]`, calling a **pre-instantiated API class**, with the service also registered in `base_urls`. So landing it means an API class contributed to tubular plus operator config — larger than "expose an endpoint," and worth knowing before it is scheduled. It stays deferred (§1.2); the deletion API underneath it still ships.

---

## 8. Open decisions this structure needs answered

Three, in the order they will bite. None blocks starting week 1.

1. **Does the ingest hop send extracted text or rendered text?** Gated by Verification Test 1 (§3.6) — if some block types return OLX needing interpretation, per-type extractors are needed and the only sane home for them is platform-side, next to `content_adapter`, since interpretation may need platform code. `platform/adapters/extractors/` would then appear. Budget half a day per additional type, per the verification plan.

2. **Vector store and metadata store: one engine or two?** §6.2's filter-before-rank requirement (§6.3, §10.2) is much simpler if metadata filters and vectors live in one queryable store, and the isolation guarantee is the thing most damaged by getting it wrong. The structure above keeps them as sibling modules under `knowledge/` so either answer fits, but the choice should be made before week 2 and recorded as ADR-001.

3. **Which Open edX release is the target — Ulmo or Verawood?** (V-E.) Ulmo is the current stable and the safe default; Verawood is the June 2026 release and is what an operator upgrading this year will land on. This needs deciding in week 1, not week 4, because it determines which instance the five verification tests run against — and a test result recorded against the wrong release is worse than no result.

4. **Does the platform package version-lock against the service, or negotiate?** `contracts/version.py` supports either. For a single-instance MVP (§3.5) a hard lock asserted at startup is correct and cheaper. It stops being correct the moment two deployments run different versions — which is a multi-tenancy question (§3.5) and therefore deferred with it.

---

## 9. What this structure is optimised for, stated plainly

**For one engineer, 3.5 weeks, and a reviewer who will look for the seams.** It is not the structure a five-person team would build — that one would have separate repos, generated clients, and a service mesh. This one has three packages, one CI rules file, and every directory traceable to a numbered section of the design.

The trade it makes: **a slightly heavier repository in exchange for the platform boundary being un-crossable by accident.** Every other simplification available here — one package, shared dependencies, no contracts module — buys a few hours in week 1 and pays for them by making the design's central promise unverifiable. Given that the promise *"we never degrade your LMS"* is what makes CourseMate installable at a university at all, that is not a close call.

---

## Sources

Checked 2026-07-30 against `main`/`master` unless noted.

- [openedx-events — content_authoring signals](https://github.com/openedx/openedx-events/blob/main/openedx_events/content_authoring/signals.py) · [learning signals](https://github.com/openedx/openedx-events/blob/main/openedx_events/learning/signals.py) · [events reference](https://docs.openedx.org/projects/openedx-events/en/latest/reference/events.html)
- [edx-platform `content/search/handlers.py`](https://github.com/openedx/edx-platform/blob/master/openedx/core/djangoapps/content/search/handlers.py) · [`index_config.py`](https://github.com/openedx/edx-platform/blob/master/openedx/core/djangoapps/content/search/index_config.py) · [`reindex_studio.py`](https://github.com/openedx/edx-platform/blob/master/openedx/core/djangoapps/content/search/management/commands/reindex_studio.py)
- [edx-platform `xmodule/modulestore/mixed.py`](https://github.com/openedx/edx-platform/blob/master/xmodule/modulestore/mixed.py) · [`modulestore/__init__.py`](https://github.com/openedx/edx-platform/blob/master/xmodule/modulestore/__init__.py)
- [XBlock `core.py` (XBlockAside)](https://github.com/openedx/XBlock/blob/master/xblock/core.py) · [XBlock Runtime API](https://docs.openedx.org/projects/xblock/en/latest/runtime.html)
- [edx-django-utils — how to create a plugin app](https://github.com/openedx/edx-django-utils/blob/master/edx_django_utils/plugins/docs/how_tos/how_to_create_a_plugin_app.rst)
- [LiteLLM Router routing docs](https://docs.litellm.ai/docs/routing)
- [Open edX Sumac dev/operator release notes (Meilisearch + Tutor)](https://docs.openedx.org/en/latest/community/release_notes/sumac/dev_op_release_notes.html) · [release index (Ulmo current, Verawood June 2026)](https://docs.openedx.org/en/latest/community/release_notes/index.html)
- [User retirement driver scripts](https://edx.readthedocs.io/projects/edx-installing-configuring-and-running/en/latest/configuration/user_retire/driver_setup.html) · [implementation overview](https://docs.openedx.org/projects/edx-platform/en/latest/references/docs/scripts/user_retirement/docs/implementation_overview.html)
- [open-craft/xblock-ai-evaluation](https://github.com/open-craft/xblock-ai-evaluation)
