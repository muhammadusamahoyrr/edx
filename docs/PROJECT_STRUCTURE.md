# Project Structure

*Why each boundary is where it is. The short version: a folder structure is not
organisation, it is **enforcement** — every top-level split below is checked in
CI, because a promise only a reviewer enforces decays the first busy week.*

---

## Top level

```
coursemate/
├── packages/
│   ├── coursemate-contracts/   # wire schemas — both sides import; pydantic only
│   ├── coursemate-platform/    # Open edX plugin: XBlock, receivers, adapter
│   └── coursemate-service/     # FastAPI: knowledge, boundary, AI pipeline
├── eval/                       # evaluation harness — offline, own deps
├── deploy/                     # Dockerfiles, Tutor plugin
├── tools/verification/         # platform probes and stack checks
├── docs/                       # this documentation set
├── .importlinter               # six architectural contracts, enforced in CI
└── Makefile
```

**Three packages, not one**, because two artifacts ship to different places on
different schedules: a plugin baked into the Open edX image, and a service
container. One package would pull LangGraph, LiteLLM and a model client into a
gunicorn worker — precisely the coupling the topology exists to prevent.

**`eval/` is top-level with its own dependencies**, so *"evaluation never runs in
the request path"* is a packaging fact rather than a discipline.

---

## `coursemate-contracts` — the shared spine

```
coursemate_contracts/
├── auth.py          # StudentClaims: who is asking. NOT a grant of access
├── chat.py          # ChatRequest, StreamFrame, Citation
├── ingest.py        # ONE RECORD PER LEAF BLOCK
├── metadata.py      # the retrieval filter schema
├── errors.py        # abstained | preparing | unavailable — three distinct states
├── invalidation.py  # enrollment/scope notices
├── examprep.py      # Feature B data model (dormant)
└── version.py       # contract version; both sides assert at startup
```

**Dependency ceiling: pydantic.** This package is imported into LMS processes;
anything added here is added to the customer's platform.

Two files carry design guarantees in their *shape*:

- **`ingest.py` sends one record per leaf block.** The rule "two blocks are never
  merged into one chunk" is enforced by the wire format, not a conditional — the
  chunker only ever sees one block and is structurally incapable of merging.
- **`errors.py` types three failure states separately.** *"Still being prepared"*,
  *"not covered in this course"* and *"the tutor is unavailable"* mean different
  things to a student, and collapsing them into a generic error is the difference
  between a live demo and a dead one.

---

## `coursemate-platform` — inside Open edX

```
coursemate_platform/
├── apps.py                      # plugin registration; receivers per project type
├── adapters/content_adapter.py  # THE ONLY MODULE THAT TOUCHES THE MODULESTORE
├── events/
│   ├── cms_receivers.py         # content_authoring — fires in Studio
│   └── lms_receivers.py         # learning — fires in the LMS
├── tasks/                       # Celery: ingest, bootstrap, reconcile
│                                #   __init__ imports all three — autodiscovery
│                                #   registers nothing from an empty package init
├── drift.py                     # sweep decision logic, pure (no Django/Celery)
├── client/                      # server-to-server only; jwt, http, endpoints
├── xblock/
│   ├── tutor_block.py           # mint + render. Never relays an answer
│   ├── smoke_block.py           # permanent environment smoke test
│   └── static/                  # templates, CSS, browser stream client
├── management/commands/         # coursemate_reindex, coursemate_reconcile
├── models.py                    # resumable index state, failed_ingestions
├── migrations/                  # their tables (absent until the sweep read them back)
└── settings/                    # common.py sets defaults and NEVER raises
```

**Dependency ceiling: `XBlock`, `httpx`, `PyJWT`, `pydantic`.** Deliberately
absent: every AI library. Contract 2 enforces it, and this is the single most
valuable rule in the repository — it makes *"CourseMate cannot degrade your LMS"*
structurally true rather than aspirational.

**`content_adapter.py` is one file, not a package.** The promise *"if the store
changes, that module changes and nothing else does"* is checkable at a glance only
while it is one file.

**Receivers are split by process, not by event type**, because
`PluginSignals.CONFIG` is keyed by project type. An earlier draft put every
receiver in the CMS and would have missed enrollment events entirely.

**`settings/common.py` never raises.** Two versions of that file took the platform
down — one read `ENV_TOKENS` (which doesn't exist at COMMON stage), one raised on
an unset key. Plugin settings load during Django startup: an exception there stops
the LMS for every course on the instance, including those that never enabled
CourseMate.

**`smoke_block.py` is kept, not thrown away.** When a new release or rebuilt image
breaks block loading, it says in thirty seconds whether the fault is the platform
or CourseMate. It has no dependencies beyond XBlock, so a failure is unambiguous.

---

## `coursemate-service` — outside Open edX

```
coursemate_service/
├── main.py              # FastAPI app; three credential classes on separate routers
├── config.py            # every credential lives here, none in the platform
├── api/
│   ├── deps.py          # JWT verify, rate limit
│   ├── chat.py          # SSE encoding — NO AI logic
│   ├── ingest.py        # service credential only
│   └── invalidation.py  # immediate revocation
├── ai/
│   ├── pipeline.py      # retrieve → gate → generate → cite. Never raises
│   ├── client.py        # LiteLLM Router: retries, cooldowns, fallbacks
│   ├── retrieval.py     # ContextProvider, via the boundary
│   ├── context.py       # the protocol RAG plugs into
│   └── prompts.py       # trust tiers; retrieved text is quoted data
├── boundary/
│   ├── interface.py     # the contract — states only what is implemented
│   ├── impl.py          # identity → scope → filter → audit, every call
│   └── authz.py         # enrollment re-derivation; fails closed
├── knowledge/
│   ├── store.py         # FTS5 index; write → verify → swap
│   ├── rerank.py        # coverage + proximity + title
│   └── cache/           # SPECIFIED, NOT WIRED — see its README
├── ingestion/chunking.py
└── proposals/           # DORMANT — schema only, see its README
```

**Why `boundary/` sits between `ai/` and `knowledge/`.** Four things must happen
on every data access — resolve identity, check scope, filter before ranking,
audit. Scattered across callers, a new one forgets one and the failure is
invisible: results look plausible because they *are* plausible, just drawn from a
wider set than the student may see. Contract 3 makes forgetting a CI failure.

**Why `api/chat.py` contains no AI logic.** The pipeline yields frames; the route
encodes them. That split is what let retrieval land in Phase 6 without the API
changing shape.

---

## Conventions for code that is designed but not running

| Convention | Applies to | Example |
|---|---|---|
| Real module, schema only, nothing imports it | Deferred subsystems whose data shape is the contribution | `proposals/` |
| Real module + **README stating it is unwired** | Rules that must exist before the feature | `knowledge/cache/` |
| Not created at all | Work needing a surface that doesn't exist | Instructor review UI |

**No `future/`, `wip/` or `v2/` directories.** A parking lot is where scope goes
to become invisible.

Every dormant directory carries a README naming what it is, which design section
specifies it, and why it is not wired. During this project's final review, an
unwired cache with passing tests was found to read as an active security control —
the README convention exists because of exactly that.

---

## The six contracts

| # | Contract | Defends |
|---|---|---|
| 1 | Only `content_adapter` touches the modulestore | One module changes if storage changes |
| 2 | **Platform imports nothing AI-shaped** | The LMS cannot be degraded |
| 3 | Reasoning reaches knowledge only via the boundary | The security chokepoint |
| 4 | Nothing imports the dormant proposal queue | Dormant means dormant |
| 5 | Runtime never imports the eval harness | Evaluation stays offline |
| 6 | Contracts import nothing but pydantic | The shared spine stays light |

Contracts 1 and 3 are scoped to **direct** imports: `tasks → content_adapter →
xmodule` is the intended architecture, and an early version forbade the very
design it was defending. Contract 2 stays transitive, because an indirect import
still drags an AI library into the LMS image.

```bash
make check     # contracts + 66 tests, seconds, no Open edX required
```
