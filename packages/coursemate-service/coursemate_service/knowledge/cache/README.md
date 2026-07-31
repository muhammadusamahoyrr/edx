# Cache tiers — specified, NOT wired

**Nothing in the request path calls this code today. There is no response cache.**

That is stated first because the tests in `test_cache_keys.py` pass, and a passing
test named *"personal results are never cacheable"* reads as an active security
control. It is not one yet — it passes vacuously, because there is nothing to
protect. Anyone auditing this project should know that before trusting the name.

## Why it exists before it is used

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

## Before wiring a cache

Read §6.4 and §10.2, then call `policy.assert_cacheable()` on every write path.
The tests already encode the expected behaviour; they stop being vacuous the
moment a cache exists.
