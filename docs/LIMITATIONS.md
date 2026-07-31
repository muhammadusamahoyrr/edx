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

## 4b. Citations do not survive a page reload

Found while capturing screenshots. During a live answer, citations render as
links under the response. After a reload they are gone.

`persist_turn` saves the question and answer text to `Scope.user_state`;
`Citation` objects are sent by the browser but not stored, and `student_view`
re-renders history as plain turns. So the live answer is cited and the persisted
one is not.

**Not critical** — every answer is cited at the moment it is given, and the
underlying retrieval is unaffected — but it undercuts the product's central claim
for any student who refreshes. The fix is small: persist the citation list
alongside each turn and render it in `student_view`. Recorded rather than fixed
because handoff mode is for bugs that break behaviour, not for features.

---

## 5. Ingestion gaps

- **Publish-triggered incremental indexing is wired but unverified end to end.**
  The receiver enqueues, and the code path is exercised by unit tests, but a full
  publish→index→retrieve cycle has not been demonstrated. Bootstrap indexing is
  verified.
- **No reconciliation sweep running.** There is **no unpublish event** in
  `openedx-events`, so an unpublished unit remains indexed until a sweep removes
  it. The sweep is designed (§5.4) and not scheduled. **Until it runs, the tutor
  can cite content students can no longer see** — the one live correctness gap in
  the system.
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

1. **Schedule the reconciliation sweep.** The only live correctness gap: the tutor
   can currently cite unpublished content.
2. **Move the rate limiter and authz cache to Redis.** Cheap, and removes a silent
   correctness failure the moment anyone scales out.
3. **Extend the gold set with paraphrase questions.** Without this, embeddings
   cannot be justified by measurement — the benchmark is saturated.
4. **Add embeddings as a second retriever, merged with BM25.**
5. **Verify publish-triggered indexing end to end.**
6. **Exercise a hosted provider**, including a forced outage to test fallback.
7. **Cross-encoder reranking**, now measurable against the lexical baseline.
8. **Feature B**, which is a project of its own.

---

## 9. Honest summary

CourseMate works end to end and is measured: it retrieves real course content,
answers with citations, abstains correctly, and enforces enrollment. Every claim
in the benchmark report is backed by an executable run.

It is **not production-ready**. The nearest gaps are operational (single-replica
assumptions, no reconciliation sweep) rather than architectural — the boundaries
have held under six phases of change, and the seams designed for retrieval and
model swapping both absorbed real replacements without the API moving.

The most useful thing this project produced was not the tutor. It was the
evidence: four significant bugs — a confidence gate that could never fire, 88% of
a course silently unindexed, a platform-breaking settings import, and an FTS5
injection — were found by measurement and tooling, not by review. Three of them
were invisible in normal use and two looked like success.
