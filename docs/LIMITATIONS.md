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

## 6. Not built at all

| Feature | Status |
|---|---|
| **Feature B — exam prep** | Contracts only. No storage, extraction, CLO tagging, or UI |
| **Instructor loop** | Proposal queue schema exists, dormant. No struggle signals, review UI, or notifications |
| **XBlockAside** | Tutor must be added per-unit by hand. The aside would auto-attach, filtered to `vertical` |
| Instructor-visible opt-in state | `coursemate_reindex --all` now indexes only courses carrying the tutor block, matching what the sweep already required. There is no UI showing an admin which courses opted in — `--all` prints the counts and nothing else |
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
