# Known Limitations and Future Work

*What does not work, stated plainly. A limitation named is a limitation someone
can plan around; a limitation omitted is one they discover in front of students.*

---

## 1. Retrieval is lexical only

**The largest quality gap.** BM25 matches words, not meaning. *"What causes
processes to hang forever?"* will not find a lesson that only says "deadlock".

Semantic retrieval was the design's plan (§6.1). It was not built because it needs
an embedding provider and none was configured — and shipping the lexical half is
what §6.1 anticipates as one of the two retrievers in a hybrid.

**Why the benchmark cannot currently show this.** recall@3 = 1.000 on the gold
set, because the questions share vocabulary with the lessons that answer them.
The gold set needs paraphrase questions before embeddings can be justified by
measurement rather than by argument.

**Next:** add an embedding retriever *alongside* BM25 and merge — not replace.
Lexical keeps exact technical terms that embeddings blur.

---

## 2. No hosted inference

Verified only against local `qwen2.5:7b` on CPU: **24 s to first token against a
2 s budget.** LiteLLM makes the provider a config value, so this is one setting —
but no hosted provider has been exercised, which means retries, cooldowns and
cross-vendor fallback are **implemented and untested against a real outage**.

---

## 3. Single-replica assumptions

Two components are per-process and will behave incorrectly with a second replica:

| Component | Problem | Fix |
|---|---|---|
| Rate limiter (`api/deps.py`) | In-memory — N replicas allow N× the limit | Redis |
| Authz cache (`boundary/authz.py`) | In-memory — revocation clears one replica | Redis |
| SQLite index | Local file — replicas see different indexes | Shared store or read replicas |

None is hard to fix. All are silent if deployed unnoticed, which is why they are
listed first among operational gaps.

---

## 4. No response cache

`knowledge/cache/` contains the key derivation and the isolation policy, with
tests — **and nothing calls it.** The tests pass vacuously. Its README says this
before anything else, because a green test named *"personal results are never
cacheable"* otherwise reads as an active control.

The rules exist ahead of the cache because they encode bugs already found once: a
staff-scope answer served to a student, and personal uploads surviving in a cache
after every filter was correct.

---

## 5. Ingestion gaps

- **Publish-triggered incremental indexing is verified end to end** — and was
  broken until it was. The receiver enqueued correctly and the worker discarded
  every message with `Received unregistered task`, because the platform package
  was never pip-installed in the worker containers: `docker cp` puts code on the
  path but installs no dist-info, so the `cms.djangoapp` entry point was missing
  and the app never reached `INSTALLED_APPS`. Publish returned 200 throughout.
  Fixed by installing the package **in the image** (Tutor plugin patch
  `openedx-dockerfile-post-python-requirements`) and by importing every task
  module from `tasks/__init__.py`, without which Celery's autodiscovery
  registers nothing from a task *package*. Verified live: republishing a unit
  restored 5 blocks to the served index in under 4 seconds.
- **The reconciliation sweep runs** — nightly at 03:30, and again on every
  publish. Verified: a real `celery beat` process loads the entry
  (`<ScheduleEntry: coursemate-nightly-reconcile … <crontab: 30 3 * * *>>`),
  `reconcile_all` queues the courses the service actually serves, and each
  per-course sweep completes. The dedicated `coursemate-beat` **container** is
  configured in the Tutor plugin but **UNVERIFIED**: it starts from the openedx
  image, so it needs the image rebuild that installs the package, and that build
  had not finished at time of writing. Until it has, run the sweep from cron or
  by hand — the schedule itself is proven. There is still **no unpublish event** in
  `openedx-events`, so the sweep remains the only mechanism that can detect
  unpublished content, and a window therefore remains: between an unpublish and
  the next sweep, the tutor can still cite content students can no longer see.
  Publishing anything in the same course closes it immediately. **That window
  cannot be eliminated without a platform event** — claiming otherwise would be
  dishonest. Verified live on DemoX: unpublishing a unit removed nothing on its
  own, and the sweep then took the served index from 221 blocks to 216.
- **The nightly course list comes from the service, not the platform.**
  `CourseIndexState` is written only by the bootstrap task, so a list built from
  it was empty on a stack serving 231 chunks: the sweep ran across zero courses
  and reported success. The service is asked what it serves instead.
- **The sweep will not delete more than half a course in one run** without
  `--force`. `iter_course_leaves` yields nothing when a course read fails, which
  is indistinguishable from "everything was unpublished"; without the cap, one
  bad modulestore read wipes the index and logs success.
- **Import/rerun handlers deferred.** An imported course needs a manual reindex.
- Video transcripts are recognised but transcript extraction is a stub.

---

## 6. Not built at all

| Feature | Status |
|---|---|
| **Feature B — exam prep** | Contracts only. No storage, extraction, CLO tagging, or UI |
| **Instructor loop** | Proposal queue schema exists, dormant. No struggle signals, review UI, or notifications |
| **XBlockAside** | Tutor must be added per-unit by hand. The aside would auto-attach, filtered to `vertical` |
| Socratic mode | Prompt written, never evaluated |
| Query rewriting | Not built — *"that algorithm from week 4"* will retrieve poorly |
| Cross-encoder reranking | Lexical reranker only |
| Multi-tenancy | `tenant` threaded through; single-valued |
| User retirement | Endpoint designed; tubular pipeline registration not done |

---

## 7. Evaluation limitations

- **n=18 questions, generation sampled at 6.** Indicative, not settled.
- **Single rater — the author.** No blind scoring; the person who built the
  retriever wrote the gold set.
- **Groundedness is a token-overlap floor**, not entailment. A paraphrased but
  supported sentence can score as unsupported.
- **Latency figures are not comparable across runs** — model cache state and the
  answer/abstain mix both move the medians.
- **One course, one model.**

---

## 8. Future work, in the order I would do it

1. **Move the rate limiter and authz cache to Redis.** Cheap, and removes a silent
   correctness failure the moment anyone scales out.
2. **Extend the gold set with paraphrase questions.** Without this, embeddings
   cannot be justified by measurement — the benchmark is saturated.
3. **Add embeddings as a second retriever, merged with BM25.**
4. **Exercise a hosted provider**, including a forced outage to test fallback.
5. **Cross-encoder reranking**, now measurable against the lexical baseline.
6. **Shorten the unpublish window** by subscribing to a platform unpublish event
   — which means proposing one upstream, since none exists.
7. **Feature B**, which is a project of its own.

---

## 9. Honest summary

CourseMate works end to end and is measured: it retrieves real course content,
answers with citations, abstains correctly, and enforces enrollment. Every claim
in the benchmark report is backed by an executable run.

It is **not production-ready**. The nearest gaps are operational (single-replica
assumptions, a sweep interval rather than an event) rather than architectural — the boundaries
have held under six phases of change, and the seams designed for retrieval and
model swapping both absorbed real replacements without the API moving.

The most useful thing this project produced was not the tutor. It was the
evidence: four significant bugs — a confidence gate that could never fire, 88% of
a course silently unindexed, a platform-breaking settings import, and an FTS5
injection — were found by measurement and tooling, not by review. Three of them
were invisible in normal use and two looked like success.
