# CourseMate

**An AI tutor for Open edX that answers only from the course it lives in — and says so when it can't.**

CourseMate embeds a chat tutor inside an Open edX lesson. It answers from that
course's published content, cites the lesson each answer came from, and abstains
when the course doesn't cover the question rather than improvising from a model's
general knowledge.

Built and verified against a real Open edX **Ulmo** instance — two courses,
282 indexed chunks, a live Celery worker and a nightly sweep container.

---

## What it does

| | |
|---|---|
| **Grounded** | Answers are built from retrieved course content, never from the model alone |
| **Cited** | Every answer links back to the lesson block it came from |
| **Abstains** | Below a confidence threshold it says *"that doesn't appear to be covered in this course"* — in **3 ms**, before any model call |
| **Authorised** | Enrollment is re-derived from Open edX on every request; a valid token is not sufficient |
| **Access-aware** | Staff-only blocks never enter the index; cohort- and paid-track blocks are filtered per caller **inside the SQL**, so unauthorised content is never even a candidate |
| **Marks what it cannot support** | Sentences the retrieved material does not back are flagged in the answer, never silently rewritten |
| **Reads video** | Transcripts resolved through the platform's own resolver, which handles both storage paths Open edX uses |
| **Opt-in** | A course is indexed only if its staff added the tutor block. `--all` skips the rest and says how many |
| **Non-invasive** | No core Open edX changes, no fork. Installs as a plugin |

### The load-bearing architectural decision

**The LMS is never in the answer path.** The XBlock mints a short-lived JWT and
returns; the browser streams from the CourseMate service over a same-origin path
routed at the ingress.

Measured: **3 concurrent 4-second generations produced zero LMS log lines and
103 ms of LMS CPU — against a 118 ms idle baseline.** The obvious design (proxy
the stream through the XBlock) would have held one gunicorn worker per student
for the full generation. A worker pool is exhausted by *occupancy*, not
computation.

```
Browser ──1── XBlock.mint()  →  JWT          (0.115 ms; LMS released)
   │
   └────2──── /coursemate/api/chat  ──→  CourseMate service
              (same-origin, routed by Caddy, never enters an LMS process)
                                   │
                    retrieve → gate → LiteLLM → SSE stream → browser
```

---

## Screenshots

*Real Open edX lesson, real course content, `qwen2.5:7b` via Ollama. No mock.*

**Grounded answer with citations back to the source lessons** — these persist across a page reload

![Grounded answer citing the Cohorts lesson](docs/screenshots/02-grounded-answer-with-citation.jpg)

**Abstention — the behaviour that matters most**

![Tutor declining an off-topic question](docs/screenshots/03-abstention.jpg)

Asked *"What were the main causes of the French Revolution?"*, the tutor replies
**"That doesn't appear to be covered in this course."** — instantly, with no model
call. The confidence gate fires before generation, so refusing costs 3 ms and
nothing in spend. A tutor that answers this question is worse than no tutor: a
student cannot tell a fabricated answer from a real one.

---

## Results

Measured against the live stack, `qwen2.5:7b` via Ollama.

| | |
|---|---|
| MRR | 0.833 |
| Retrieval latency p95 | **12 ms** |
| False-answer rate (answered when it should abstain) | **0.000** |
| False-abstention rate | **0.000** |
| Citation correctness | 1.000 |
| Authorization matrix | **4/4 pass** |

### Retrieval, and why one number is not enough

The original gold set scores **recall@3 = 1.000** — which says less than it
looks like. Those questions were written while reading the corpus, so they
inherited its vocabulary. Asking *"what are XBlocks?"* of a lesson titled
**XBlocks** measures string matching.

So a second arm was added: the same content, asked in words the lessons do not
use.

| Arm | n | recall@1 | recall@3 |
|---|---|---|---|
| Original — shares the lesson's words | 12 | 0.750 | **1.000** |
| Paraphrase — deliberately avoids them | 10 | 0.200 | **0.300** |

**And the part that matters more than the drop:** two of the ten paraphrase
questions retrieve the *wrong* lesson at a score **above** the confidence
threshold — so they are answered rather than abstained. The gate catches a weak
match; it does not catch a confident match on wrong content. That failure is
invisible to a student, because the answer is fluent, cited, and grounded — in
the wrong lesson.

This is the honest state of lexical-only retrieval, and it is the baseline
semantic retrieval has to beat.

Full methodology, the reranker A/B, and the bugs measurement exposed:
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).

---

## What measurement found that review did not

Every one of these passed code review and returned success while being wrong.
They are listed because they are the strongest argument for how this was built,
not in spite of being embarrassing.

| Defect | Why it survived |
|---|---|
| The confidence gate **could never fire** | `score = raw / best` makes the top hit exactly 1.0 for every query. The gate existed, had tests, and was structurally incapable of triggering. |
| A 226-block course **served 26 blocks** | Each ingest batch swapped itself in and deactivated its predecessors. Nothing failed; the content silently vanished. |
| Celery **discarded every task** while Publish returned 200 | The package was `docker cp`'d, so pip installed no dist-info, so the entry point was absent, so the app never reached `INSTALLED_APPS`. Only visible in a worker log. |
| The enqueued reindex **activated nothing** | It never sent `is_final`, so the swap never ran. It reported `indexed == total`, because that counts what the service accepted, not what went live. Found only when a second course made the totals stop matching. |
| A partition lookup was **a write, not a read** | Its docstring says it assigns a group and *persists* that decision. Called per token mint, it would have enrolled students into A/B experiment groups for opening the chat box. Caught by reading the platform source rather than trusting the function name. |
| A frame type the UI rendered and **nothing ever emitted** | `UNSUPPORTED_CLAIM` shipped in the contract, was handled in the browser, and had no producer — so the documented check did not exist. |

The common shape: **the failure path returned success.** That is why the probes
in [`tools/verification/`](tools/verification/) assert on what a user would
check, and why every "empty" result in them is disambiguated — a control that
fails closed hides its own failure.

---

## Quick start

Requires Docker, and Open edX via [Tutor](https://docs.tutor.edly.io/).

```bash
# 1. install the plugin
cp deploy/tutor-plugin/coursemate.yml "$(tutor plugins printroot)/"
tutor plugins enable coursemate
tutor config save

# 2. build the service image
docker build -f deploy/Dockerfile.deps    -t coursemate/deps:1      .
docker build -f deploy/Dockerfile.service -t coursemate/service:0.1.0 .

# 3. bake the plugin INTO the Open edX image -- not optional
#    `docker cp` puts code on sys.path but installs no dist-info, so pip's
#    cms.djangoapp entry point is absent, the app never reaches INSTALLED_APPS,
#    Celery autodiscovers nothing, and every enqueued task is discarded while
#    Studio's Publish button still returns 200.
rsync -a packages "$(tutor config printroot)/env/build/openedx/coursemate/"
tutor config save && tutor images build openedx     # ~30 min with a warm cache

# 4. start, migrate, index
tutor local start -d
tutor local run cms ./manage.py cms migrate coursemate_platform
tutor local run cms ./manage.py cms coursemate_reindex \
--course course-v1:YourOrg+Course+Run --inline
```

Add `coursemate_tutor` to the course's Advanced Module List, drop the block into
a unit, publish. Full walkthrough: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/PROBLEM_STATEMENT.md`](docs/PROBLEM_STATEMENT.md) | The problem this attacks, and the ones it deliberately leaves alone |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flows, diagrams, the decisions and their reasons |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Installation, configuration, operations, troubleshooting |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Evaluation methodology, results, bugs found by measurement |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | What does not work, and what would be built next |
| [`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md) | Repository layout and why each boundary exists |
| [`docs/TECHNICAL_SUMMARY.md`](docs/TECHNICAL_SUMMARY.md) | Engineering decisions, trade-offs, lessons |
| [`docs/CourseMate_Complete_Design.md`](docs/CourseMate_Complete_Design.md) | Full design document — every decision with its reason and rejected alternative |

---

## Development

```bash
make install     # venv + editable installs
make check       # architecture contracts + 127 tests, no Open edX required
```

Tests are self-contained — a clean checkout runs green with no environment setup.
CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same two
commands on Python 3.11 and 3.12, **plus a job that introduces a deliberate
contract violation and fails the build if the contract does not catch it.**

Tests run in seconds without a platform. Tests that need Tutor live in
`packages/coursemate-platform/tests/platform/` and run at milestones — a suite
that needs a platform is a suite nobody runs.

### Architecture enforced in CI

`.importlinter` turns six design promises into build failures:

1. Only `content_adapter` touches the modulestore
2. **The platform package imports nothing AI-shaped** — this is what makes *"CourseMate cannot degrade your LMS"* structurally true rather than aspirational
3. Reasoning reaches knowledge only through the `CourseIntelligence` boundary
4. Nothing imports the dormant proposal queue
5. Runtime packages never import the evaluation harness
6. Contracts import nothing but pydantic

Contract 2 has been verified to fail on a deliberate violation — a green contract
that cannot fail is decoration.

---

## Status

**Runs end to end on a live Open edX Ulmo stack, and is measured. Not published,
not production-deployed.**

| | |
|---|---|
| Verified working | Ingestion on publish, bootstrap, nightly sweep, video transcripts, retrieval, citations, abstention, enrollment re-derivation, block-level access, claim marking, two-course isolation |
| Runs on | Tutor 21.0.8 / Open edX Ulmo, single node |
| Not tried on | Other releases, Kubernetes, Learning Core, multiple replicas |
| Not installable yet | Nothing is on PyPI or a public registry — this is a clone-and-build repo |

**Known gaps, in the order they would be fixed** — the full list with reasons is
in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md), which is deliberately harsher
than this section:

1. **Retrieval is lexical only.** Measured cost: recall@3 falls from 1.000 to
   0.300 on paraphrased questions, and two of ten are answered confidently from
   the wrong lesson.
2. **No hosted model provider has ever been exercised.** Retries, cooldowns and
   cross-vendor failover are implemented and untested against a real outage.
3. **One service replica only.** Rate limiting, the authz cache and LiteLLM
   cooldowns are shared through Redis; the SQLite index is still a local file.
4. **Feature B (exam prep) is data shapes only** — cut deliberately, against a
   list written in week one so the decision was not made under deadline.

The evaluation is 22 covered questions on two courses, scored by one person who
also wrote the retriever. It is indicative, not settled, and it is reported that
way throughout.

## License

MIT — see [`LICENSE`](LICENSE).

The Open edX demo course used for development and benchmarking is published by the
Open edX community under **CC BY-NC-SA** and is **not redistributed here** — it is
fetched at setup time. Its Non-Commercial and Share-Alike terms do not compose
with MIT, which is why it is downloaded rather than vendored.

Open edX® is a registered trademark of edX Inc. CourseMate is an independent
plugin, modifies no Open edX source, and is not endorsed by or affiliated with
edX Inc.
