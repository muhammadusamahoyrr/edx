# CourseMate — Probe 7

| Item | Value |
|---|---|
| Captured (UTC) | `2026-08-04 20:46:49` |
| Host platform | `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.35` |
| Python (in container) | `3.11.8` |
| Django | `5.2.11` |
| openedx-events | `10.5.0` |

## Probe 7 — Video transcripts and block-level access

### Objective

Determine, on this instance: which transcript resolver exists, whether a video block yields usable text, whether any block carries a visibility or group restriction, and whether the partition lookup returns tokens that match what blocks are keyed on.

### Method

Call the shipped `content_adapter` functions inside the CMS, against the published branch, on a real course. Read-only throughout.

### Commands executed

```bash
tutor local run cms python /openedx/probes/probe_07_access_and_transcripts.py
```

### Source locations

- `packages/coursemate-platform/coursemate_platform/adapters/content_adapter.py`
- https://github.com/openedx/edx-platform/blob/master/xmodule/partitions/partitions_service.py

### Evidence

| Observation | Value |
|---|---|
| transcript resolver found | `True` |
| resolver module | `xmodule.video_block.transcripts_utils` |
| published video blocks in course | `10` |
| videos carrying a transcript pointer | `10` |
| videos yielding extractable text | `0` |
| staff-only leaf blocks | `0` |
| group-restricted leaf blocks | `2` |
|   restricted block-v1:OpenedX+DemoX+DemoCourse+type@html+block@1b6d50cee32745e58c29e10e2789fcad | `18587404:1819362822` |
|   restricted block-v1:OpenedX+DemoX+DemoCourse+type@html+block@1fa75541b9b9433b98153b2f36a0da23 | `18587404:205150518` |
| probe user | `admin` |
| user_group_tokens(admin) | `('50:1',)` |
| active partitions on course | `2` |
|   partition 18587404 | `Content Groups scheme=cohort` |
|   partition 50 | `Enrollment Track Groups scheme=enrollment_track` |
| course_has_tutor() | `True` |
| tutor blocks found | `1` |
| leaves yielded | `221` |
| leaves by type | `{'html': 198, 'problem': 23}` |
| leaves carrying group tokens | `2` |

### Conclusion

- **CONFIRMED** — `get_transcript` resolves from `xmodule.video_block.transcripts_utils` on this release.
- **CONFIRMED** — 10 video(s) carry a transcript pointer but NONE yielded text. The resolver is reachable and still returning nothing — this is the real failure, not an authoring gap.
- **CONFIRMED** — 2 block(s) carry group_access. The query-time filter has real data to be tested against.
- **CONFIRMED** — Partition lookup works: 1 token(s) for this user.
- **CONFIRMED** — Opt-in check returns True for a course containing 1 tutor block(s). `--all` will include this course.
- **CONFIRMED** — iter_course_leaves yields {'html': 198, 'problem': 23}. A 'video' entry here is the end-to-end proof that transcripts reach ingestion; its absence means they do not.

### Implications for the AI Tutor

- If section A found no resolver, add this release's module path to _TRANSCRIPT_MODULES before anything else — every video is silently lost.
- If section D shows partitions but no tokens, the block-level access filter is hiding restricted content from EVERY caller, including those entitled to it. Fails closed, so it leaks nothing, but it is not working.

### Assumptions and limitations

- One course, one user, one release. Nothing here generalises to another instance without re-running it there.
- Section C reads group_access from the modulestore. If enrollment-track gating is applied at render time instead, this probe cannot see it — compare against the Block Structure API called as an audit user.

---

