# Session handoff — 2026-08-19

Written at the end of a long session so the next one does not re-derive any of
it. **`CLAUDE.md` is still the authority on the stack; read it first.** This file
covers only what changed in this session and what is left open.

Everything below was verified live unless it says otherwise.

---

## 1. Live state, as measured just now

```
distro PID1 age : 05:29:39          (no restart during the session)
keepalive       : 1 process         cm-keepalive-long, pid 2308
containers      : 13/13 up
lms image       : 84ee9b36068c      <- NEW, adopted 2026-08-18 14:11
prev3 rollback  : 21.0.8-indigo-prev3 -> 6088aabf8d01   (intact)
:80 listeners   : 2                 (no Caddy socket issue)
OEX101 bank     : 5 questions, 35 marks
```

`cm-keepalive-long` is a detached `setsid` process holding the WSL distro open.
**Leave it running.** Without it the distro terminates during unattended gaps and
kills long builds — see §6.

---

## 2. What shipped: the MathJax fix

Commit **`ba9b245`**, pushed to `origin/block-access-and-transcripts`, built into
image **`84ee9b36068c`**, adopted by all five Open edX containers, and verified in
a real browser.

**The defect:** `renderAnswer` never handed its node to the page's MathJax, so
LaTeX arriving *after* window load stayed as literal text — `\(Z = \lnot{(C(A+B))}\)`
on screen.

**Important correction, do not re-learn it:** a hard reload (F5) makes math render
*without* the fix, because MathJax 2.7.5 typesets the whole document at window
`load` and the XBlock initialises earlier at DOM `ready`. Only the **live-stream**
and **runtime conversation-switch** paths were broken. An F5 test is
non-discriminating.

**The fix** (`tutor.js`): `typesetMath(node)` queues `["Typeset", MathJax.Hub, node]`
once per finished answer at three call sites — history render, chat stream `done`,
plan stream `done`. Never per token. `sanitizeMath` then strips
`javascript:` hrefs and `url()` styles from the generated MathML, because the
host MathJax has **no `Safe` extension** and `\href{javascript:…}` was a live XSS
vector.

Verified in browser as `cm_student` on DemoX: 4 MathJax nodes in the streamed
turn, `leaked: []`, `navigation.type = "navigate"` (no reload), citations intact.

`tutor.js` in the running LMS and CMS: sha1 `3fba69c3d371`, 95950 bytes,
`typesetMath=4`, `sanitizeMath=3`.

---

## 3. Uncommitted working tree — three separate pieces

```
 M docs/openapi.json                                        (generated)
 M packages/coursemate-contracts/coursemate_contracts/examprep.py
 M packages/coursemate-platform/.../xblock/static/js/src/tutor.js
 M packages/coursemate-platform/tests/js/test_practice_loop.mjs
 M packages/coursemate-platform/tests/js/test_study_plan_ui.mjs
 M packages/coursemate-service/coursemate_service/ai/planner.py
 M packages/coursemate-service/tests/test_planner.py
 M packages/coursemate-service/tests/test_study_plan_api.py
?? packages/coursemate-service/tests/test_pack_merge.py
?? tools/extract/merge_packs.py
```

**All tests pass:** `make check` → 6 contracts kept / 0 broken, 1256 backend
passed + 3 xfailed, 301 browser passed across 9 suites, `docs/openapi.json`
current.

### (a) Mastery badge — `tutor.js`, `test_practice_loop.mjs`

Not written by me; found already in the tree, dated after the image build. It
updates the in-memory mastery snapshot and repaints the `.cm-plan-mastery` badge
after a self-check, so badges stay correct without a reload (`renderPlan` only
runs from the plan fetch, so nothing else refreshed them).

I reviewed it and fixed two defects, both with reverse-checked regression tests:

- **never synthesise a snapshot.** `mastery = mastery || {clos: []}` dropped
  `offering_id`, which `MasterySnapshot` requires — the next study-plan POST
  would 422 instead of being ignored. Now guarded on `if (mastery)`.
- **exact CLO-ID match.** `indexOf(cloId) === 0` on label prose made `CLO-1` a
  prefix of `CLO-10`. `planItemNode` now stamps `data-clo` and the updater
  matches the attribute.

`test_practice_loop.mjs`: 28 → 33 tests.

**Left alone deliberately** (do not "fix" without asking): the increment ignores
`difficulty_band` and `source`, so it lands on the first row matching `clo_id`.
The badge total is unaffected because `masteryBadge` sums across bands, and it
self-heals on reload. It is a design call, not a bug to patch.

### (b) `StudyPlan` shortfall — contract, planner, UI

`StudyPlan` now carries `requested_marks` and `planned_marks`, both read off
`PlanReport` through one `_plan()` constructor so the two can never drift.
`renderPlan` prefers them and falls back to deriving when absent (older service).

**Additive only — no `CONTRACT_VERSION` bump** (policy is "bump on breaking
changes"). `docs/openapi.json` regenerated.

**Planner allocation logic is untouched** and a test pins that.

> Correction to an earlier claim of mine: the UI **already** showed a shortfall
> (`.cm-plan-unspent`). This change makes the numbers authoritative rather than
> browser-derived — it did not introduce them.

### (c) `merge_packs.py` + `test_pack_merge.py` — new, 21 tests

Operator tooling for combining several past papers into one pack before
`/packs/load`. **Never run against production.** See §4 for why it exists.

---

## 4. OEX101 question bank — investigation closed

**The 35-mark bank is complete and valid. Nothing was lost.** Re-ran the
extractor over the source PDF: 5 questions, 35 marks, byte-identical to the DB,
0 low-confidence, 0 missing marks, both pages had a text layer. Extraction,
multipart parsing (`2(b)`), wrapped lines, marks, tagging, dedupe and insertion
are all lossless.

**The source is a test fixture, not a real paper.** `tools/fixtures/oex101_final_2024.pdf`
(1,343 bytes) is generated by `make_exam_pdf.py` to exercise the extractor. Its
CLO list is fixture data too — `confirmed_by: "dr-lee"` appears nowhere but the
test suite.

**There are no real OEX101 past papers.** Searched the repo, both WSL homes, all
containers, and the Windows `Downloads`/`Documents`/`Desktop` trees.

### ⚠ The trap — do not load `eval/datasets/generation_pack.json`

It looks like the answer: 20 questions, 165 marks, CLO-3 covered, right
`offering_id`. It is **hand-written eval data wearing a past paper's clothes**:

- `source_doc_id: "oex101-final-2024.pdf"` — hyphens; **that file does not exist**
- `content_sha256: null` — no document was ever hashed
- claims `extraction_method: "digital"`, pages 1–6, questions 1–20 for a paper
  that has never existed
- `confirmed_by: "eval-set"`; declares a **CLO-4** production does not have

Loading it would fabricate a past paper *and* destroy the real bank
(`load_pack` replaces). Leave it in `eval/`.

### CLO-3 is mis-specified, not under-covered

Production CLO-3 is *"Configure and troubleshoot a Tutor-based Open edX
deployment."* The course's own stated objectives are history, community,
governance and contribution — nothing about Tutor. Across 55 active OEX101
chunks: `troubleshoot` 0, `docker` 0, `configur` 1, `deploy` 1.

So CLO-3 has no past-paper questions, no course material to ground it, and the
practice generator returns `ABSTAINED` for it (`_find_source` needs a real
past-paper seed). **The fix is to correct the CLO list with instructor
confirmation — not to find content.** That decision is the user's.

### Two facts that govern any future load

1. **`load_pack` REPLACES.** It deletes every question and CLO for the offering
   before inserting. Loading a second paper alone destroys the first. Multi-paper
   banks are built by merging then loading **once** — that is what `merge_packs.py`
   is for.
2. **A 70-mark plan cannot be filled from 35 marks of past papers**, and if study
   plans stay past-paper-only (they should), no amount of official Open edX/Tutor
   material changes that. Official material can make an outcome *practisable*,
   never *plannable*.

### Deferred design, deliberately not built

A `QuestionOrigin` enum (`PAST_PAPER` / `COURSE_MATERIAL`) with a default-deny
filter in `search_questions`, a separate `search_practice_seeds` boundary method,
and a `/packs/load` guard. **Do not build it yet** — its only beneficiary is
CLO-3, and CLO-3 should not exist in its current form. Revisit after the CLO list
is corrected. There is exactly one read path into the bank
(`boundary.search_past_questions`), which is the seam it would attach to.

---

## 5. Open items, in the order I would take them

1. **Commit the working tree.** Three logical pieces (§3) — could be one commit
   or three. All tests green.
2. **Decide on the OEX101 CLO list** — instructor-confirmed correction so the
   outcomes match what the course teaches. Data, not code.
3. **Rebuild + adopt** when ready. The working `tutor.js` now differs from the
   deployed image by both the mastery-badge work and the `StudyPlan` UI change,
   so the next image carries both. Use `tools/ops/rebuild_image_logged.sh` then
   `tools/ops/adopt_new_image.sh`; keep `cm-keepalive-long` alive throughout.

### Known limitations, unchanged and not authorised for work

- markdown **tables** render as raw pipes in answers
- practice-question card uses `body.textContent = answer` (no rich rendering)
- `typesetMath` on detached nodes is not optimised
- mid-stream conversation-switch persistence bug
- Phase 2 `section_path`, the `clearNode` fix
- `\cssId` / `\class` residual in the MathJax sanitiser
- **Groq credential still unrotated** — revoke at the provider
- authz cold-start timeout stays at 5.0; bundle with the next deployment

---

## 6. Environment traps learned this session

- **The WSL *distro* terminates during unattended gaps, even though the *VM*
  stays up.** `vmIdleTimeout=-1` in `.wslconfig` governs the VM, not the distro.
  Symptom: containers restart mid-build; `ps -o etime= -p 1` resets while the VM
  boot_id does not. Mitigation is the detached `setsid` keepalive
  (`cm-keepalive-long`), proven across 100 s and 180 s gaps with byte-identical
  container `StartedAt`. **Do not modify `.wslconfig`.**
- **A hung image build is Docker failing to *start* containers**, not a network
  problem: containers sit in `Created`, the shim spawns, `docker run` blocks in
  `futex_wait_queue`, and the daemon log stays silent. Recovery is
  `wsl --shutdown`, then budget ~2 min for docker and 1–2 more for the LMS.
- **Always use script files under `tools/ops/`** invoked with `MSYS_NO_PATHCONV=1`.
  Inline `wsl -- bash -c '...'` from Git Bash mangles quoting and drops variables —
  it produced at least four wrong readings this session before I stopped doing it.
  Delete temp scripts afterwards, and check `git status` *after* deleting them.
- **`docker exec` needs `-i`** for heredoc input, or you get silent empty output.
- **`git status` from inside WSL reports ~141 modified files.** That is a CRLF
  artefact — WSL git lacks `core.autocrlf=true`. **Windows git is the accurate
  view.** The image's `tutor.js` is the committed file with CRLF endings, which is
  why its sha1 differs from `git show HEAD:…` output.
- Running pytest from the repo root gives 8 bogus "async def not supported"
  failures. The package's own config sets asyncio mode — **use `make check`**.

---

## 7. Working method the user expects

- **Verify, don't assume.** A green response is not evidence; check the thing the
  user would check. New tests were reverse-checked against pre-fix copies in the
  scratchpad to prove they actually fail.
- **Simple English.** Short sentences, common words, still exact.
- **Never push, publish, load, rebuild, restart or deploy without being asked.**
- **Read `CLAUDE.md` STATE first** — a wrong restart wipes the install silently.
- Report faithfully: if a claim of mine turned out wrong, say so plainly and move
  on. Three of my claims were corrected this session (the F5 path, the test
  count, and the study-plan shortfall).
