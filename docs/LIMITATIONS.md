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

**Now measured, not argued.** A paraphrase arm was added to the gold set on
2026-08-05 — ten questions asking for the same content in words the lessons do
not use. Against the live index:

| Arm | n | recall@1 | recall@3 |
|---|---|---|---|
| Original (shares the lesson's words) | 12 | 0.750 | **1.000** |
| Paraphrase (avoids them) | 10 | 0.200 | **0.300** |

And the sharper finding: **2 of the 10 score ABOVE the confidence threshold while
retrieving the wrong lesson**, so they are answered rather than abstained. The
gate catches a weak match; it does not catch a confident match on wrong content.
See BENCHMARKS §3.5.

**The largest instance of that same failure was conversational, and is now
fixed.** Before 2026-08-12 the retriever never saw the conversation, so 7 of 12
multi-turn cases answered confidently from the wrong lesson — one at score 1.000.
Searching the reconstructed question took multi-turn recall@3 from 0.333 to 0.917
and the wrong-and-answered count from 7 to 1, with both single-turn arms
unchanged (BENCHMARKS §3.8). That does not fix the lexical gap above; it removes
a second cause that was being read as one.

**Next:** add an embedding retriever *alongside* BM25 and merge — not replace.
Lexical keeps exact technical terms that embeddings blur.

---

## 2. Hosted inference is now the primary; failover verified once, cross-vendor still absent

> **Updated 2026-08-14.** The heading used to read *"Hosted inference exercised
> once; failover still untested"*. Both halves changed on the same day: a hosted
> provider is now the live primary, and the chain has been driven by a real
> outage. What is *still* missing is narrower and is stated at the end of this
> section. The original text follows, because the local numbers below are still
> the ones every earlier benchmark was measured against.

Everything routinely verified — chat, Feature B, and the browser run of
2026-08-12 — uses local `qwen2.5:7b` on CPU, against a 2 s budget. The two paths
were measured separately and their numbers are **not interchangeable**:

| Path | TTFT p50 | TTFT p95 | n | Where |
|---|---|---|---|---|
| Chat / retrieval | **24 s** | 110 s | 6 generated | BENCHMARKS §3.3 |
| Feature B generation | **9.7 s** | 106 s | 4 | BENCHMARKS §3.6 |

Feature B is faster at the median because it makes one short completion from a
fixed prompt, where chat carries history and a longer context. Both miss the
budget by an order of magnitude. LiteLLM makes the provider a config value, so
switching is one setting.

**A hosted provider HAS been exercised**, contrary to what this section used to
claim: a Groq run on 2026-08-11 (§5.2) cut time-to-first-token from 187–301 s to
13–15 s and exposed a real tool-schema defect. That key was subsequently pasted
into a chat transcript, is treated as compromised, and has not been rotated — so
no hosted provider has been used since, and the numbers above are all local.

**Failover has now been driven by an actual outage (2026-08-14).** The live
topology is `strong` → OpenRouter (hosted), `cheap` → local Ollama.
`tools/verification/failover_probe.sh` disabled the hosted provider and the local
model answered with the same three citations while the UI showed `DEGRADED`;
disabling both produced `UNAVAILABLE` rather than a fabricated answer. Numbers in
BENCHMARKS §3.11.

Three things remain genuinely missing, and they are smaller than the sentence
this replaced:

* **No cross-vendor failover between hosted vendors.** `fallback_model` is still
  empty, so the chain is hosted → local. One hosted outage is survivable; the
  degraded answer is a 7B model on CPU.
* **Generation has no fallback at all.** `build_generation_fallback_chain`
  requires a registered `fallback`, and there is none, so a `strong` outage makes
  practice generation UNAVAILABLE. Deliberate — see ADR-0001 — but it means
  Feature B is strictly less available than chat.
* **Cooldowns and `allowed_fails` are untuned** against real free-tier behaviour.
  They have never been exercised by sustained load.

### `provider_failures_total` cannot see a silent degradation — CLOSED 2026-08-14

**Resolved.** `degraded_answers_total` now exists and is incremented in
`pipeline.py` at the one place that already knows a fallback answered — beside
the `DEGRADED` frame, on the same condition. A primary degrading on every request
now moves a counter instead of only a badge in the student's browser.

`provider_failures_total` is unchanged and still means *"generations that failed
entirely"*, which is a real and separate question. The two together are what an
operator needs: one for "the tutor is down", one for "the tutor is quietly worse".

`test_degradation_controls.py` pins that the counter is wired rather than merely
declared — this repository has shipped a counter, a setting and a version lock
that nothing called, which is why declaring it was never the hard part.

The original finding is kept below, because it explains *why* the obvious counter
was not enough.

---

Found while writing the failover probe. The counter is incremented in
`pipeline.py` only inside its `except` blocks, and LiteLLM's Router resolves the
fallback internally — so when a fallback **succeeds**, no exception reaches the
pipeline and the counter does not move.

| scenario | DEGRADED frame | `provider_failures_total` |
|---|---|---|
| primary down, fallback answers | yes | **+0** |
| whole chain down | no — `ERROR` instead | **+1** |

The metric means *"generations that failed entirely"*. Its **name** says provider
failures. A primary that is silently degrading every request — the condition an
operator most needs to be paged on — moves it by exactly zero, and the only
visible symptom is a badge in the student's browser.

The fix is a separate `degraded_answers_total` incremented where the DEGRADED
frame is emitted. ~~Not done, because it is a behaviour change and the work that
found it was documentation-only.~~ **Done 2026-08-14** — see the note above.

**And "untested" was doing more work than it looked like.** Two defects sat in
that untested path until 2026-08-10, both found by driving a real LiteLLM Router
with mocked completions rather than by reading the code:

- **The fallback provider was serving half of all healthy traffic.**
  `fallback_model` was registered as a second deployment named `strong`, and
  deployments sharing a `model_name` are load-balanced, not chained. Measured
  against litellm 1.94.1: **20 of 40 calls went to the secondary vendor** with
  both providers up. Nothing failed — two working providers both answer — so
  quality and spend split between vendors invisibly, and the setting did not do
  the one job it existed for. A fallback is now its own deployment name reached
  through `fallbacks=`, with the different-vendor entry ahead of `cheap`, which
  shares the primary's vendor and therefore its outage.

- **Every healthy answer would have been flagged DEGRADED.** The check was
  `provider_used not in settings.strong_model` — a substring test against the
  configured string. Providers return versioned ids, so a healthy
  `claude-opus-5-20260514` answering a configured `anthropic/claude-opus-5` is
  not a substring. It now reads the Router's own deployment id, which survives
  streaming, and treats "could not identify" as unknown rather than as degraded.

Both are the failure shape this project keeps finding, in the one area nobody
could see: **a green path doing the wrong thing.** Neither is reachable by
reading this repository alone — they only appear when a real Router runs, which
is why `tests/test_model_routing.py` builds one.

What remains genuinely untested is the real thing: an actual vendor outage,
against actual credentials.

---

## 3. Single-replica assumptions — mostly closed

| Component | State |
|---|---|
| Rate limiter (`api/deps.py`) | ✅ **Redis sliding window**, shared across replicas |
| Authz cache (`boundary/authz.py`) | ✅ **Redis**, and `invalidate()` clears every replica |
| LiteLLM cooldowns | ✅ **Redis** — a dead provider is discovered once, not per replica |
| SQLite index | ❌ **Still local.** Replicas would see different indexes. |

Redis costs no new infrastructure: it is already Celery's broker in every Tutor
deployment. We use **db 1**; db 0 is the platform's broker, and sharing a
keyspace with it means a careless `FLUSHDB` while debugging takes out both.

**Degradation is deliberately asymmetric.** With `redis_url` unset or Redis
unreachable, all three fall back to per-process state — correct for one replica,
which is what they were before. The rate limiter fails **open** (abuse control:
denying every student because a cache is down trades a small risk for an
outage); the authz cache treats a Redis failure as a **miss** and asks the
platform, which remains the source of truth. Authorization itself still fails
**closed**, unchanged.

**Verified live** (`tools/ops/deploy_shared_state.sh`): two `_RateLimiter`
instances standing in for two replicas shared one budget and blocked at request
21 of 20.

The SQLite index is the honest remainder — it needs a shared store or read
replicas, which is a real piece of work rather than a config change.

---

## 3.1 Two defaults that were wrong

- **`require_grounding` defaulted to `False`.** Every abstention behaviour — the
  confidence gate, `ABSTAINED`, `PREPARING` — sits behind that flag, so a fresh
  install answered from the model's own knowledge instead of the course. It was
  False through Phase 5 for a good reason (no retriever existed, so everything
  would have abstained); the reason expired with Phase 6 and the default did not
  follow it. **A safety control that must be switched on is not a control.** Now
  `True` in both `config.py` and the Tutor plugin.

- **`max_output_tokens` had drifted to 200** (service default 800, plugin 250,
  `config.yml` 200) and answers were being cut off mid-sentence with no
  indication. A truncated answer is indistinguishable from a complete one to the
  student who reads it — it simply stops, and the natural reading is that the
  tutor did not know the rest. The `DONE` frame now carries `truncated`, the
  browser renders a notice, and the limits are aligned at 800. Lower it
  deliberately for a slow local model; do not leave it low by accident.

---

## 3.2 Claim verification — now emitted, and it is a floor

`FrameType.UNSUPPORTED_CLAIM` was in the contract from v1 and rendered by
`tutor.js` from v1, and **nothing ever emitted it.** A UI branch that can never
fire is the same defect `boundary/interface.py` names about declaring four tools
while implementing one: a reader concludes the check exists.

It now fires. `ai/verify.py` splits the answer into sentences and asks whether
each sentence's content words appear in the retrieved material; those that do
not are marked, never rewritten — the student has already read the text, and
changing it under them is worse than saying which part to doubt.

**Verified live** (`tools/verification/claim_verify_live.sh`), against the
deployed service:

```
answer with one ungrounded sentence -> citations=1  unsupported=1
   MARKED: Kubernetes schedules replica pods across availability zones.
fully grounded answer (control)     -> unsupported=0
```

**Citations narrowed at the same time.** The pipeline emitted one per retrieved
chunk, so a citation meant "we searched this" rather than "the answer used
this" — three authoritative links under a sentence none of them supported. They
now narrow to the chunks the answer drew on, falling back to all of them when
nothing overlaps, because §8.5 makes citation mandatory.

**The limitation, stated as plainly as the benchmark states it.** This is
token overlap, not entailment. It catches a sentence about material we never
retrieved — what a model produces when it falls back on its own knowledge. It
does **not** catch a fluent contradiction built from the right words: *"deadlock
cannot occur when locks are ordered"* and *"deadlock can occur when locks are
ordered"* score identically. `BENCHMARKS.md` already says this about
token-overlap groundedness; this inherits the same ceiling at inference time.

The upgrade is an NLI model — `LettuceDetect` (MIT, ModernBERT, returns
character spans with confidence) drops in behind the same frame. Deliberately
not first: it needs torch, which is the objection `knowledge/rerank.py` already
raises against the cross-encoder, and adding it before this floor existed would
leave us unable to say what it bought.

`claim_support_threshold` (0.4) is a starting point, not a calibrated number.

---

## 4. The response cache is wired, and its hit rate is near zero

**Updated 2026-08-12.** This section used to say no response cache existed and
that `knowledge/cache/` passed its tests vacuously. Both were true for six
phases. The response tier is now wired (`response_cache.py`, first turn only) and
verified in a real browser: 74,973 ms → 133 ms on a repeated first-turn question,
with zero budget charged, so the provider genuinely was not called
(BENCHMARKS §3.10). `assert_cacheable` now runs on every write, which is what
makes the *"personal results are never cacheable"* test an active control rather
than a name.

The embedding and metadata tiers are still specified and **not** wired.

**The honest limitation is now the opposite one: it will almost never hit.** The
key carries the caller's effective scope *including `student_id`*, so a hit
requires the same student to ask the same question as a first turn twice — and
their conversation history persists between attempts, which makes the second ask
a follow-up rather than a first turn. In normal use the cache is close to inert.

Sharing between students with an *identical* authorization scope is what would
make it pay, and it is defensible: retrieval is fully determined by scope, so two
callers with the same offering, roles and group tokens retrieve the same chunks
and would receive the same answer. It has not been enabled because it is a
security trade, and §10.2 is specifically about caching being how isolation
quietly fails after every filter is already correct. Verified live that scope is
load-bearing here rather than theoretical: this deployment's students really do
carry group tokens (`cm_student` mints `["50:1"]`).

Two smaller gaps, both known:

- **Only the index version invalidates.** A prompt edit, a model swap or a τ
  change is not in the key and is bounded only by the 1 h TTL.
- **`history` is not in the key but is passed to `build_messages`**, so a client
  sending `history=[]` and the browser sending `history=[echo]` map to one key
  while building marginally different prompts. Same question, same retrieval,
  same scope — a fidelity gap, not an isolation one.

### Reviewed 2026-08-14: kept as-is, deliberately

An audit raised the obvious question — a mechanism that provably never fires is
177 lines plus a 617-line test file buying nothing, so either fix the key or
delete it. **Live counters after real traffic: `cache_hits_total` 0,
`cache_misses_total` 1.** Both alternatives were considered and neither taken:

* **Dropping `student_id` from the key** is the change that would make it work,
  and it is a change to the exact surface §10.2 names as the place isolation
  quietly fails once every filter is already correct. The reasoning above says
  it is *defensible*, not that it is *verified*, and the difference is a body of
  isolation tests nobody has written. Not a change to make in an audit pass.
* **Deleting it** would throw away a mechanism that is correct, browser-verified
  (74,973 ms → 133 ms, 0 charged) and cheap to leave in place. The cost of an
  inert cache is a Redis GET per first turn.

So it stays, switched on, and this section is the record that its near-zero hit
rate is a known property rather than a fault waiting to be found. What is **not**
acceptable is quoting the cache as a latency or cost saving: it is not currently
providing either.

One thing did change. A cache HIT used to skip `abstentions_total`, so the
abstention rate would have fallen as the cache warmed and read as the tutor
answering more. Replayed abstentions are now counted.

---

## 4.1 The spend ceiling is enforced on an estimate here

**Added 2026-08-12.** A student may spend 100,000 tokens per course per UTC day
(`cm:budget:{offering}:{student}:{YYYYMMDD}`), checked before the provider call.
Measured cost is 660–1,295 tokens per answer depending on how long the
conversation has grown, so roughly 75–150 answers a day. BENCHMARKS §3.9.

Three caveats worth stating plainly:

1. **The number charged here is an estimate, not the provider's.** Verified by
   probing the running router: `ollama_chat/qwen2.5:7b` reports no usage on any
   stream chunk, so the ledger falls back to counting characters over the real
   prompt and the real answer (`chars // 4`). It is a measurement of work done
   rather than a request counter, and it agreed exactly with the observed ledger
   delta when reconstructed — but it is not the provider's meter, and on a hosted
   provider that reports usage the reported figure is used instead.
2. **The overshoot is not contract-bounded.** The check runs before generation
   and the charge lands after, so the last permitted question can exceed the
   ceiling by one answer. The reply is capped by `max_output_tokens`; the
   *prompt* is not capped by anything (§6, "Per-request input limits").
3. **A cache hit is free and is not refused.** The C2 read sits before the
   ceiling check, so an over-budget student can still be served a cached answer.
   That costs nothing, so it does not weaken the spend bound — but it does mean
   "over budget" is not the same as "no answers".

A Redis outage degrades to **per-process** counting, not to unlimited and not to
refusing everyone: worst case becomes `replicas × ceiling`, which is exactly the
ceiling on this single-replica deployment.

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
- **The reconciliation sweep runs, and beat dispatching it is now VERIFIED**
  (`tools/verification/beat_probe_derived.sh`). From a container started off an
  image carrying the package: the entry loads from plugin settings
  (`crontab: 30 3 * * *`), beat fires it, and the CMS worker executes it —

  ```
  Scheduler: Sending due task coursemate-nightly-reconcile
  reconcile_all succeeded: {'courses_queued': 1}
  sweep course-v1:OpenedX+DemoX+DemoCourse: live=222 indexed=222 orphans=0
  ```

- **RESOLVED 2026-08-05: the `coursemate-beat` container now runs.** The openedx
  image was rebuilt with the plugin baked in (29 minutes, not the hours the notes
  predicted — buildx had a warm cache). The container starts, loads
  `crontab: 30 3 * * *` from plugin settings, and was observed dispatching
  `reconcile_all` to a worker that executed it. `beat_container_probe.sh` is the
  production-path proof; `beat_probe_derived.sh` remains as the no-rebuild route.

- **Environment caveat, and it is a real one:** the Docker daemon in this WSL
  distro restart-loops every ~2–3 minutes (dockerd uptime resets while the distro
  stays up for hours and memory is idle). Every container cycles with it. Work
  observed completing is still real — restarts happen between operations — but
  **an unattended nightly job cannot be called dependable on this host.** Beat
  re-reads its persistent schedule on restart, so 03:30 would still fire; that is
  a property of the scheduler, not evidence the host is stable.

- *(Historical, resolved above: the deployed image was stock, so beat would have
  found no schedule entry and scheduled nothing — silently, since an empty
  schedule is not an error. Recorded because it is the exact shape of failure
  this project keeps finding: a green path that does nothing.)*

- There is still **no unpublish event** in
  `openedx-events`, so the sweep remains the only mechanism that can detect
  unpublished content, and a window therefore remains: between an unpublish and
  the next sweep, the tutor can still cite content students can no longer see.
  Publishing anything in the same course closes it immediately. **That window
  cannot be eliminated without a platform event** — claiming otherwise would be
  dishonest. Verified live on DemoX: unpublishing a unit removed nothing on its
  own, and the sweep then took the served index from 221 blocks to 216.
- **The ENQUEUED bootstrap never activated what it wrote** (found and fixed
  2026-08-05). `bootstrap_course` called `send_leaves` without `run_id` or
  `is_final`, so the service never verified and never swapped: every chunk
  landed INACTIVE, the course kept serving the previous version, and each run
  leaked a whole copy of the course into the index. The task returned
  `{'indexed': 222, 'total': 222}` throughout, because that counts what the
  service ACCEPTED, not what became active.

  The `--inline` path always set both flags, which is why every reindex done by
  hand looked correct. The enqueued path is the one that runs in production —
  the Studio button, the query-time backstop, `--all`.

  **It only surfaced because a second course was imported** and the totals
  stopped matching: 454 chunks across 2 versions with 227 active. With one
  course the numbers were self-consistent and wrong.

  A second defect sat behind it: `last_usage_key` was never cleared on success,
  so the next run resumed from the last leaf of the finished one, sliced the
  walk to nothing, sent no batches, and reported success. Verified before
  fixing: `position in walk 221 of 221, leaves a resumed run sends: 0`.

  Fixing the first alone would have caused a worse one. A resumed run sends only
  the remaining tail, so swapping to a fresh version would activate a fraction
  of the course while reporting a complete run — the 226-indexed-26-served
  failure through the resume path. `CourseIndexState.run_id` (migration 0002)
  persists the version so a resumed run continues it and the pointer flips once.

  Verified live (`tools/verification/bootstrap_swap_probe.sh`), driving the real
  Celery task: `454 chunks / 227 active / 2 versions` became
  `227 / 227 / 1 version`.

- **The nightly course list comes from the service, not the platform.**
  `CourseIndexState` is written only by the bootstrap task, so a list built from
  it was empty on a stack serving 231 chunks: the sweep ran across zero courses
  and reported success. The service is asked what it serves instead.
- **The sweep will not delete more than half a course in one run** without
  `--force`. `iter_course_leaves` yields nothing when a course read fails, which
  is indistinguishable from "everything was unpublished"; without the cap, one
  bad modulestore read wipes the index and logs success.
- **Import/rerun handlers deferred.** An imported course needs a manual reindex.
- **Video transcript extraction VERIFIED end to end**, on one video:

  ```
  _video_transcript          -> 583 chars
  iter_course_leaves         -> 222 leaves, 1 of type video
  reindex                    -> 222 blocks, 227 chunks, 0 failed
  search "campus-wide deployments" -> that video chunk, score 1.000
  ```

  The query phrase appears nowhere else in DemoX, so the top hit proves the
  answer came from the transcript rather than from an html page on the same
  topic. Attached with `tools/verification/add_test_transcript.sh`, through
  `edxval.api.create_or_update_video_transcript` — the same path a real upload
  takes — not by writing a file into the media volume, which would have tested
  the filesystem and said nothing about whether the platform can find it.

- **The other 9 DemoX videos still yield nothing, and that is the course's data,
  not our code.** Probe 7 (`docs/Probe7_Access_And_Transcripts.md`): resolver
  found in `xmodule.video_block.transcripts_utils`, 10 published videos, **10
  carrying a transcript pointer, 0 yielding text** before the fix above.
  Diagnosed with `tools/verification/transcript_diagnose.sh`:

  | Case | What the block has | What happens |
  |---|---|---|
  | 2 of 3 sampled | `edx_video_id` set, edx-val row present, **file absent from `/openedx/media/video-transcripts/`** | `FileNotFoundError` |
  | 1 of 3 sampled | `transcripts={}`, `sub=''` | `NotFoundError` — genuinely no transcript |

  The DemoX import creates edx-val transcript rows pointing at `.srt` files it
  never ships, and `transcripts_info['sub']` is the literal string
  `non_existent_dummy_file_name`, so the contentstore has nothing either.
  **The code is not the problem; the course has no transcripts.** Repairing one
  video (above) proved that, and the remaining nine are left as they are — they
  are useful evidence of what a broken media volume looks like in the logs.

- **`get_transcript` skips its own fallback on `OSError`.** It falls back to the
  contentstore only on `NotFoundError`, so an edx-val row whose file is missing
  raises straight out and the contentstore path is never tried. We do **not**
  reach around that: doing so would make the tutor cite transcripts the
  platform's own video player cannot display. It is logged at WARNING instead,
  because a missing file is an operator-fixable storage fault, unlike an
  unauthored transcript.
- **A supported block yielding no text is now logged.** It was silent, which is
  how every video block disappeared without trace: `video` is on
  `SUPPORTED_LEAF_TYPES`, so it never reached the unsupported-type branch.

---

## 5.1 Block-level access — implemented, partly unverified

Course-level isolation was never the whole problem. Within one course, Open edX
restricts blocks two ways, and neither is cleared by publishing:

| Restriction | Handling | Verified? |
|---|---|---|
| `visible_to_staff_only` | Dropped at **index time** — absolute, no student may ever see it | Logic tested; DemoX has 0 such blocks, so unexercised live |
| `group_access` (cohorts, enrollment track) | Carried per chunk, filtered **at query time in the SQL** | **VERIFIED live, both directions** |

**Live evidence** (`tools/verification/access_filter_live.sh`, against the served
index of 226 chunks):

```
chunk 'Visible to Content Group A'  token 18587404:1819362822
  caller WITHOUT the group: 8 hits, restricted chunk hidden = True
  caller WITH the group   : 9 hits, restricted chunk shown  = True
chunk 'Visible to Content Group B'  token 18587404:205150518   (same result)
RESULT: PASS
```

Both halves are asserted on purpose. Hiding it from an unentitled caller is the
security half; **serving it to an entitled one is the half a blunt index-time
filter would have broken** — a student who paid must still receive what they paid
for. A test that only checked the first would pass just as well against a filter
that hid the content from everybody.

**Why the two are handled differently.** Staff-only is absolute, so it never
enters the index. Group restrictions are conditional — a verified-track student
*should* receive verified-only content — so filtering them at index time would
have protected audit students by breaking the product for the ones who paid.
They are therefore stored in a `chunk_groups` side table and resolved against the
caller inside the retrieval query, alongside tenant and offering (§6.3).

**What is not verified**, and it is the load-bearing half:

- `content_adapter.user_group_tokens` reads the caller's groups through
  `get_all_partitions_for_course(course, active_only=True)` +
  `get_user_partition_groups(course_key, partitions, user, "id")` — the exact
  pair `UserPartitionTransformer` uses, so our filter agrees with courseware
  rather than approximating it. **VERIFIED live** (probe 7): returns `('50:1',)`
  for `admin` against two active partitions — `50 Enrollment Track Groups`
  (scheme `enrollment_track`) and `18587404 Content Groups` (scheme `cohort`).
  The `_django_user` resolution works. It fails closed — any error yields no
  groups, so the caller sees unrestricted content only.
- **VERIFIED live: enrollment-track membership does surface through this path**,
  as partition 50. The open question is narrower than it was — whether a *block*
  restricted to a paid track carries partition 50 in `group_access`, which DemoX
  cannot answer because its 2 restricted blocks are both cohort-restricted
  (partition 18587404), not track-restricted.
- **Course staff are not exempt.** `admin` holds no content-group token, so the
  filter hides both cohort-restricted blocks from them. Courseware grants staff
  a bypass through a separate access layer that this filter does not consult.
  Fail-closed and harmless for students; surprising for staff.
- **`PartitionService.get_user_group_id_for_partition` must not be used here.**
  It assigns and *persists* a group when the user has none, so calling it per
  mint would enroll students into split-test experiment groups as a side effect
  of opening the tutor. An earlier draft of this code did exactly that.
- **The transcript resolver moved between releases** —
  `xmodule.video_block.transcripts_utils` through Sumac,
  `openedx.core.djangoapps.video_config.transcripts_utils` after. Both are tried,
  because a hard import of either raises at Celery startup on the other, which is
  a dead worker rather than a missing feature. `get_transcript` confirmed to
  return `(content, filename, mimetype)` and to try edx-val before contentstore.
- Whether enrollment-track (paid) gating actually surfaces in `group_access`
  rather than being applied at render time. If it is render-time only, the audit
  case is still open. Cross-check: read `group_access` via the modulestore and
  compare against the Block Structure API called as an audit user.
- `XBlockUser` → Django user resolution in `tutor_block._group_tokens` uses
  `_django_user`, which is private platform API.

**Group membership is the one claim taken from the token rather than
re-derived**, because re-deriving it service-side would mean hard-coding
partition and group ids that are per-instance configuration. Staleness is bounded
by the token TTL (5 minutes) and there is no revocation event for it, unlike
enrollment — which is still re-derived per call and still fails closed.

---

## 5.2 The exam-prep agent — still dark. Feature B itself is not.

Feature B and the agent layer landed on 2026-08-10. **They have since diverged,
and the distinction is the point of this section:** Feature B is verified working
in a browser; the *agent* is the part that still ships dark.

**Feature B — implemented, deployed, verified, and measured.** As of 2026-08-12 it
runs end to end on the live stack: a real PDF extracted and CLO-tagged offline,
loaded through the service-credentialed endpoint, and served to an enrolled
non-staff student in a real browser — budgeted study plan, generated practice
question with provenance, and correct abstention. See BENCHMARKS §3.6 and §3.7.
Measured at n=4, which demonstrates the pipeline rather than establishing a rate.

**The agent — implemented and tested offline only:** the tool registry (identity
refused rather than overridden, strict schemas, three outcomes not two), the loop's
failure rules, the per-tool confidence gate, the mastery memory layer with
idempotent writes, and the local stdio MCP server. 838 backend + 93 browser tests,
6 contracts.

**`agent_enabled` defaults to `False`**, so a default install routes exam prep to
the deterministic path and no agent code runs. That is the inverse of the
`require_grounding` lesson in §3.1 and the same principle applied the other way: a
*safety control* that must be switched on is not a control, and a *new subsystem*
that must be switched on is one nobody enables by accident.

**Now run against a real model, once — and the news is mixed.** On 2026-08-11,
after repairing the local Ollama install, the real `ExamPrepAgent` ran end to end
against `qwen2.5:7b` on CPU, with a real exam pack (2 CLOs, 1 question).

*Tool selection is good.* The model issued four distinct, well-formed calls and
built a genuinely structured filter — exactly what §7.6 argues records are for:

```
get_clos               {}
get_mastery            {clo_id: CLO-1}
get_mastery            {clo_id: CLO-2}
search_past_questions  {clo_id: CLO-1, exam_type: final, year_from: 2023,
                        min_marks: 10, limit: 5}
```

No repeated calls, no invented `student_id`/`offering_id`, no malformed
arguments, and the answer correctly said mastery was unknown rather than
inventing it. **n=1, one course, one model** — indicative, not a measurement.

*Latency is catastrophic on CPU, far worse than predicted.*

| | |
|---|---|
| Time to first token | **301.5 s** (target: < 2 s) |
| Total turn | **342.7 s** |
| One planning call | ~20–50 s |
| Chat, single token | 25 s cold / 2.3 s warm |

The pre-build flag said "5–15 s realistically on a hosted model". On a local 7B
CPU model it is **150× the target**. The fast path is untouched — abstention still
costs milliseconds, because the gate fires before any tool loop — but the answered
path is unusable for a live demo on this hardware. It needs a hosted provider, and
that is the measurement still missing.

### Run against a hosted provider (Groq, 2026-08-11) — and the bug it exposed

Pointing the agent at `groq/llama-3.3-70b-versatile` changed the picture
completely, but only after it surfaced a defect that had made the whole
strict-schema story false.

**The tool schemas never reached any provider.** `Tool.json_schema()` emitted the
schema under `input_schema` — Anthropic's key — while the runner wrapped it in
OpenAI's `{"type": "function", "function": …}` envelope, where the key must be
`parameters`. Every provider therefore saw tools declared with **no parameters at
all**. Groq validates server-side and rejected every call with
`additionalProperties 'clo_id', 'exam_type', … not allowed`, naming the very
fields the schema was meant to declare. Ollama does not validate, so locally it
looked like the model was inventing argument names — which is exactly what it was
doing, because it had nothing to follow.

That also retracts an earlier diagnosis in this file: the invented
`learning_outcome` / list-valued `clo_id` were **not** model weakness. They were
the absence of a schema. `registry.py` carries the full note.

**After the fix**, over seven runs (one transient provider failure, six clean):

| | local `qwen2.5:7b` | `groq/llama-3.3-70b` |
|---|---|---|
| iterations | 6 (always capped) | **2–3** |
| time to first token | 187–301 s | **13.3–15.2 s** |
| total turn | 222–419 s | **13.8–15.7 s** |
| LLM planning | 145–310 s | **~4.4 s** |

Of that ~14 s, **~9.5 s is one-time LiteLLM Router construction** paid at process
start, not per turn — so a warm service is roughly **4–5 s to first token**. Still
over the < 2 s target, but a usable demo rather than an unusable one.

Batching also works on this model: one run issued two `search_past_questions`
calls in a single planning turn, which the runner's existing `for call in calls`
handled unchanged.

**Two caveats, both real.** `--live` tool-selection accuracy came back **0.44**,
but that run exhausted Groq's free-tier daily cap (98,716 of 100,000 tokens) and
rate-limited cases counted as misses — it is a contaminated floor, not a
measurement, and it needs re-running with headroom. And one run in seven failed
on a transient provider error, which is why `run_agent_eval.py` and the profiler
now report provider failures loudly instead of showing a suspiciously fast run
with zero iterations.

**The model expands to fill `max_iterations`. Measured on the local model.**

`qwen2.5:7b` emits one tool call per planning turn — proven directly: asked
*"Call BOTH get_clos AND get_mastery now, in a single message, as two tool calls
together"*, it returns exactly one. So six iterations buys six tool calls, and the
runner's existing `for call in calls` (what frontier models exercise) never fires.

The obvious fix was to need fewer calls, so `get_clos` and `get_mastery` were
merged into `get_plan_context`, and `search_past_questions.clo_id` was widened to
accept a list. Both work. **Neither reduced the round trips**, because the model
simply spent the freed rounds on something else:

| Run | Iterations | What the spare rounds did |
|---|---|---|
| before merge | 6 | one search per outcome |
| after, run 1 | **3** | (sent a CLO list to a `str` field → turn died; fixed) |
| after, run 2 | 6 | called `get_plan_context` twice more, identically |
| after, run 3 | 6 | relaxed `min_marks` 10 → 5 → 0 on an outcome with no questions |

In runs 2 and 3 the **last two rounds returned nothing new** — a duplicate call,
and three empty searches. That is ~50–100 s of the turn buying zero information.

So the binding constraint is the cap, not the tool count. The merge is still worth
keeping — it is strictly fewer calls for the same information on any model, and it
is what made run 1's 3 iterations possible — but on this model the honest
conclusion is that `agent_max_iterations = 6` is a budget the planner will always
spend. Lowering it to 3–4 is the change the profile actually supports; it has not
been made, because it should be calibrated against a hosted model rather than
against a 7B that cannot batch.

`eval/run_agent_eval.py` still reports tool-selection accuracy as **NOT MEASURED**
by default, and a `--live` run on 2026-08-12 did not change that: against the
local `qwen2.5:7b` it timed out on nine of ten planning calls before printing a
figure, so what it produced measured timeouts rather than tool choice. Measuring
this needs a hosted model — which is the same constraint §2 describes, and the
reason `agent_enabled` ships `False`.

**Mastery is not a grade, and since 2026-08-12 it is explicitly self-reported.**
The snapshot is carried by the browser, exactly as chat history is (§3.1), so a
student can forge their own. What that buys them is worse study recommendations
for themselves: it never reaches another student, it cannot widen retrieval
scope, and enrollment is still re-derived at the boundary on every call. It must
never be read as an assessment record.

**Nothing marks the answer, and nothing claims to.** A past-paper
`QuestionRecord` carries the question text and no answer key — there is none
anywhere in the system — so the student attempts the question and then marks
their own attempt "I got this" or "Not yet". `record_attempt` was always built to
be *told*: it takes `correct` from the payload. Judging free text would mean a
second model call whose accuracy is unmeasured, which is exactly the kind of
claim §9.0 says must be measured before a student sees it.

Two consequences worth stating rather than discovering:

- **The counters measure confidence, not correctness.** A student who
  consistently overrates themselves gets a plan that under-weights their weakest
  outcome. That is a real limitation of self-assessment as a signal, not a bug in
  the recording.
- **The written answer never leaves the page.** Nothing can compare it against
  anything, so transmitting it would create a store of student prose with no
  purpose, inside the retirement boundary, for no gain.

- **An attempt at a GENERATED question is recorded against the past-paper
  question it was modelled on** — and nothing distinguishes the two afterwards.
  Documented 2026-08-15; behaviour unchanged.

  The chain is short and none of it is accidental in isolation.
  `quiz_generator.py` puts `question_id=source.question_id` on the DONE frame —
  the real `QuestionRecord`, not the variant the student read — `tutor.js` passes
  that to `record_attempt`, and `StudentMastery` increments the counter for that
  `(clo_id, question_id, difficulty_band)`.

  `ai_generated` exists on `PracticeQuestion` and is set for every generated
  question, but it stops at the service: it is not on the DONE frame, not in the
  `record_attempt` payload, not on `CLOMastery`, and not in the stored row. So
  mastery is attributed to a `question_id` the student never saw.

  **That is defensible, and it was never actually decided.** Defensible because
  the counter tracks the OUTCOME rather than the item, and the variant is
  modelled on that source and tagged to the same CLO — which is the granularity
  the planner ranks on. Never decided because there is no comment saying so, no
  test pinning it, and nothing in this document until now; `record_attempt`'s
  docstring explains every other choice it makes and is silent on this one.

  Two consequences of leaving it as it is:

  * A student who practises the same source question through several generated
    variants accumulates several attempts against that one `question_id`. They
    are distinct attempts by distinct `attempt_id`, so the idempotency guarantee
    is intact — but the record reads as repeated attempts at one past paper.
  * Nothing can later separate "answered the real paper" from "answered a
    variant", because the distinction was never stored. Recovering it means a
    contract field and a migration, which is why it is written down here rather
    than changed on the way past.

**Until this landed the loop was open**, and that is the more serious fact: the
practice card rendered a question and stopped. There was no answer field, no
submit and no caller for `record_attempt`, so `StudentMastery` was a table
nothing wrote — while the model, migration 0003, the `MasterySnapshot` contract,
the planner's weakness ranking and the agent's `get_plan_context` tool were all
built on top of it. Every student looked like a new student, permanently, and
every component involved worked correctly in isolation.

**Built since, and measured (2026-08-12).** PDF extraction (`tools/extract/
extract_pack.py`, pypdf, digital text only) and automated CLO tagging
(`ai/clo_tagger.py`) now exist and have been run end to end from a real PDF
through to scored generation — see BENCHMARKS §3.6. The earlier statement that
"producing that JSON from real papers is manual" and that the tagging prompt had
no caller is no longer true.

**Still not built for Feature B:** OCR and VLM extraction (a scanned paper has no
text layer and yields nothing — reported honestly rather than half-guessed),
difficulty derivation at extraction time (which is why `band_plausibility` cannot
be measured on a real pack), and the instructor correction UI for a mis-tagged
question.

---

## 6. Not built at all

| Feature | Status |
|---|---|
| **Feature B — OCR / VLM extraction** | Digital-text PDF extraction and CLO tagging are built and measured end to end (BENCHMARKS §3.6). A *scanned* paper still yields nothing — no OCR, no VLM. See §5.2 |
| **Instructor loop** | Proposal queue schema exists, dormant. No struggle signals, review UI, or notifications |
| **XBlockAside** | Tutor must be added per-unit by hand. The aside would auto-attach, filtered to `vertical` |
| Instructor-visible opt-in state | `coursemate_reindex --all` now indexes only courses carrying the tutor block, matching what the sweep already required. There is no UI showing an admin which courses opted in — `--all` prints the counts and nothing else |
| Socratic mode | Prompt written, never evaluated |
| Query rewriting | **Conversational reconstruction only** (2026-08-12): a follow-up carrying a pro-form is searched together with the previous student turn, which took multi-turn recall@3 from 0.333 to 0.917 (BENCHMARKS §3.8). That is not general rewriting. *"That algorithm from week 4"* still retrieves poorly — it needs to know which nouns are topical in this corpus, and so does the one multi-turn case still missed (*"Can you give an example?"*, under-specified by ellipsis rather than by a pronoun) |
| Per-request input limits | `ChatRequest.history` is capped at 20 turns, but `question` and `Turn.content` have no `max_length`. Self-limiting for spend — an oversized request exhausts the sender's own daily ceiling — but it means the ceiling's overshoot is not contract-bounded (§4.1) |
| Cross-encoder reranking | Lexical reranker only |
| Multi-tenancy | `tenant` threaded through; single-valued |
| User retirement | Endpoint designed; tubular pipeline registration not done |

---

## 7. Evaluation limitations

- **n=46 questions across five arms, generation sampled at 6.** Indicative, not
  settled — and the per-arm n is what matters, not the total: `topic_change` is
  4 cases and `usage_key_conflict` is 2, so neither supports a confident
  percentage. The headline retrieval figures cover the 28 single-turn cases only;
  the conversational arms are reported separately because a blend of arms
  measures nothing (BENCHMARKS "Dataset").
- **Single rater — the author.** No blind scoring; the person who built the
  retriever wrote the gold set.
- **The gold set is the instrument, and the temptation is to tune it.** `t02` was
  scored a miss and its expected label looked arguable; resolving it by *reading
  the block* rather than by adjusting the label kept the miss, and adding the
  block would have taken `topic_change` wrong-and-answered from 1 to 0 — an
  improvement produced entirely by editing the dataset. Three tests now pin that
  decision in both directions.
- **Offline fixtures were written from the contract, not captured from the
  client**, and that hid two shipped defects that every test passed (BENCHMARKS
  §4.5). Regression tests for both now use the verbatim wire payload.
- **Groundedness is a token-overlap floor**, not entailment. A paraphrased but
  supported sentence can score as unsupported.
- **Latency figures are not comparable across runs** — model cache state and the
  answer/abstain mix both move the medians.
- **One course, one model.**
- **The agent gold set (n=10) measures the loop, not the model.** Its four
  regression gates are decided entirely by tool outcomes, so a scripted router
  measures them exactly. Tool-selection accuracy is not measured at all until a
  provider is configured, and is reported as such. A `--live` attempt against the
  local `qwen2.5:7b` on 2026-08-12 printed `0.44` but timed out on nine planning
  calls first, so it measured timeouts rather than tool choice and is not
  recorded as a result — see BENCHMARKS §6.7.
- **The Feature B rubric detects reprinting, not rewording.** It is token overlap,
  the same honest floor `verify.py` uses. A practice question paraphrased from a
  past paper passes the duplicate check. The threshold (0.6) is calibrated on one
  course.
- **The real-PDF Feature B run is n=4** (BENCHMARKS §3.6, 2026-08-12). It is the
  first Feature B measurement whose source questions were extracted rather than
  authored, and it shows the pipeline runs end to end and passes the rubric — but
  four questions from one paper is a demonstration, not a rate. The authored-pack
  run (n=18/16/18) is still the larger sample; the two measure different things
  and must not be averaged.
- **`band_plausibility` cannot be measured on a real extracted pack.** The check
  needs a requested difficulty band, and `extract_pack.py` deliberately leaves
  `difficulty` unset (§7.6: a derived difficulty must be labelled derived, so it
  is not guessed at extraction time). It reports "not run" rather than a number.
  The earlier 0.882 was measured on the authored pack's own metadata.
- **The generation eval disables enrollment enforcement** so it can run offline.
  Enrollment is verified separately, in a real browser against the live LMS
  (BENCHMARKS §3.7), where a cross-offering request returned `403 not_enrolled`.
- **Coverage is gated on the service and contracts only** (90.9%, floor 80%).
  `coursemate_platform` sits at 33% because most of it needs a live Open edX to
  execute; blending the two produced a figure that measured how much of the code
  needs a platform rather than how well tested it is. And coverage says which
  lines ran, never whether the assertion that ran them meant anything — this repo
  has shipped 100%-covered code that returned success while doing nothing, twice.

---

## 8. Future work, in the order I would do it

1. **Move the rate limiter and authz cache to Redis.** Cheap, and removes a silent
   correctness failure the moment anyone scales out.
2. **Extend the gold set with paraphrase questions.** Without this, embeddings
   cannot be justified by measurement — the benchmark is saturated.
3. **Add embeddings as a second retriever, merged with BM25.**
4. **Force a provider outage** and prove the fallback chain. A hosted provider has
   been exercised (Groq, 2026-08-11, which exposed a real tool-schema bug), but
   the retry/cooldown/vendor-failover paths have never been driven by an actual
   failure.
5. **Cross-encoder reranking**, now measurable against the lexical baseline.
6. **Shorten the unpublish window** by subscribing to a platform unpublish event
   — which means proposing one upstream, since none exists.
7. **Turn the agent on against a hosted model** and measure the two things a stub
   cannot: tool-selection accuracy, and time to first token. Both are why
   `agent_enabled` ships `False` (§5.2). The local CPU model is not sufficient —
   it timed out on nine of ten planning calls on 2026-08-12.
8. **Derive difficulty at extraction time.** It is the one Feature B rubric metric
   that cannot be measured on a real pack, because the extractor refuses to guess
   a number whose contract says it must be labelled derived.
9. **Widen the real-PDF evaluation past n=4.** One paper is a demonstration; a
   rate needs several, ideally from different courses and layouts.
10. **OCR / VLM extraction** for scanned papers, which currently yield nothing.

---

## 9. Honest summary

CourseMate works end to end and is measured: it retrieves real course content,
answers with citations, abstains correctly, and enforces enrollment. Feature B
now runs the whole way from a real past-paper PDF to a generated, cited practice
question, verified in a browser by an enrolled student (BENCHMARKS §3.6, §3.7).
Every claim in the benchmark report is backed by an executable run.

**Three things that sound like results and are not.** The real-PDF evaluation is
n=4. `band_plausibility` is not measured on extracted packs at all. Tool-selection
accuracy for the agent is still unmeasured, and the one attempt against a local
model measured its timeouts. Each is stated wherever the neighbouring numbers
appear, rather than only here.

It is **not production-ready**. The nearest gaps are operational (single-replica
assumptions, a sweep interval rather than an event) rather than architectural — the boundaries
have held under six phases of change, and the seams designed for retrieval and
model swapping both absorbed real replacements without the API moving.

The most useful thing this project produced was not the tutor. It was the
evidence: four significant bugs — a confidence gate that could never fire, 88% of
a course silently unindexed, a platform-breaking settings import, and an FTS5
injection — were found by measurement and tooling, not by review. Three of them
were invisible in normal use and two looked like success.
