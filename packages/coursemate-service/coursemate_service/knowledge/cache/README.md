# Cache tiers

**The response tier is WIRED as of Phase C2** — `coursemate_service/response_cache.py`,
read and written by `ai/pipeline.py`, first turn only. The tests in
`test_cache_keys.py` and `test_response_cache.py` now protect something real:
`assert_cacheable` is called on every write, and the isolation tests were each
verified by deliberately removing the component they defend and watching the
right test fail.

This file previously opened by saying nothing called this code and the tests
passed vacuously. That was true for six phases and is worth remembering: the
rules below were written, and tested, before anything enforced them.

**The embedding and metadata tiers are still specified and NOT wired.** Nothing
calls `embedding_key`, `permission_key` or `clo_map_key` in the request path.

## Why it existed before it was used

Design §6.4 defines three cache tiers, and §10.2 calls one of the rules a
**security control rather than an optimisation**:

> caching is how isolation quietly fails **after** the filters are all written
> correctly.

Every filter can be right and the cache still hand one student's private upload
to another. The rules are therefore written down — and tested — *before* a cache
exists, so that whoever adds one inherits them rather than rediscovering them.

## The rules a cache must follow when it lands

| Tier | Key | Invalidated by |
|---|---|---|
| Embedding | `hash(chunk_text) + embedding_model_id` | content-addressed, never stale |
| Response | tenant + offering + course_version + **effective permission scope** + filters + normalised query + mode | any `course_version` bump; TTL ceiling |
| Metadata | `student_id + course_id` | enrollment events; **short** TTL |

Two of these encode bugs that were already found once:

1. **The response key includes the caller's effective permission scope.** Keying
   on the query string alone meant a course-staff member's answer — retrieved
   from a wider candidate set — could be served to a student who asked the same
   question.
2. **Personal-namespace results are never cached at all.** Not stored, not
   served. `policy.assert_cacheable()` raises rather than skipping silently,
   because a security control that fails quietly is one nobody notices has
   stopped working.

## Before wiring another tier

Read §6.4 and §10.2, then call `policy.assert_cacheable()` on every write path.

How the response tier satisfies the table above, for reference when the next one
lands:

| Rule | Where |
|---|---|
| effective permission scope in the key | `scope_of(student, roles, offerings)` + `group_tokens` in `applied_filters` |
| invalidated by a version bump | `course_version` = the boundary's `index_version(offering)`; a reindex changes it |
| personal results never cached | `assert_cacheable` on the write path, gated on `ContextChunk.is_personal` |
| TTL ceiling | `settings.response_cache_ttl_seconds`, default 1 h |

Two things the response tier adds that the table did not anticipate:

* **First turn only.** B1/B2 make the retrieval query depend on the previous
  turn, so a follow-up's answer is not a function of the question alone. Caching
  one would serve one student's conversation to another.
* **Degraded answers are not stored.** They came from the fallback deployment
  during an outage; caching one freezes the outage's quality in for the TTL.
