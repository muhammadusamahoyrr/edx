# CourseMate

An AI layer on Open edX: a course-grounded, cited, confidence-aware **tutor**, and
a **final exam prep** mode built around a course's CLOs and past papers.

**Target platform: Open edX Ulmo.** Pinned, not aspirational — `master` moves, and
the command this design models its bootstrap on (`reindex_studio`) dropped every
flag it had between the release the design was written against and now. See
`docs/CourseMate_Repository_Structure.md` §7b.

## What runs where

Two artifacts ship to two different places, and keeping them apart is the point.

| Package | Ships to | Holds |
|---|---|---|
| `coursemate-platform` | Open edX image (LMS + CMS + Celery) | XBlock, event receivers, ingest worker |
| `coursemate-service` | Its own container | Knowledge, boundary, agents, models |
| `coursemate-contracts` | Both, as a dependency | The wire schemas |

The platform package's dependency list is four lines long and deliberately absent
of `langgraph`, `litellm`, any model client, and any vector store. `.importlinter`
contract 2 enforces that, because *"CourseMate cannot degrade your LMS"* is the
promise that makes this installable at a university at all — and it is the promise
most likely to be broken by a small convenient import at 11pm.

**No LMS worker is held for an answer.** The XBlock mints a short-lived JWT and
returns in milliseconds; the browser streams from the service on a same-origin
path routed at the ingress. A streaming *proxy* would have held a gunicorn worker
for the whole generation, which is the same worker exhaustion the topology exists
to prevent — the pool is exhausted by occupancy, not computation (design §3.4 r3).

## Quick start

```
make install
make check          # architecture contracts + fast tests
```

`make test` needs no Tutor, no containers and no network. Tests that need a
running platform live in `packages/coursemate-platform/tests/platform/` and run at
milestones, because a suite that needs a platform is a suite nobody runs.

## Architecture rules, enforced in CI

`.importlinter` turns six design promises into build failures rather than
code-review catches:

1. Only `content_adapter` touches the modulestore (§3.3)
2. The platform package imports nothing AI-shaped (§3.4, Principle 8)
3. Agents reach knowledge only through the `CourseIntelligence` boundary (§6.5)
4. Nothing imports the dormant proposal queue (§1.2, §9.1)
5. Runtime packages never import the evaluation harness (§4)
6. Contracts import nothing but pydantic

## Documentation

| Document | For |
|---|---|
| `docs/CourseMate_Complete_Design.md` | Every decision, its reason, and the alternative rejected. **Source of truth.** |
| `docs/CourseMate_Repository_Structure.md` | Why the folders are shaped this way; the source-verification log |
| `docs/CourseMate_Build_Plan.md` | Day-level sequence, milestones, and the pre-committed cut ladder |
| `docs/Week1_Verification_Plan.md` | The open platform behaviours as bounded tests |
| `docs/adr/` | Decisions made *during* the build |

Where a document disagrees with the design, the design wins and the other is
stale. When something moves from *built* to *deferred*, search every document for
its name before closing the change — four separate inconsistencies in this set
were caused by a claim outliving the thing that supported it.
