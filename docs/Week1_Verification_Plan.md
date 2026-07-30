# Week 1 — Platform Verification Plan

*Five behaviours the design assumes but has not proven on a running instance. Each is a bounded test, not research. Together they are roughly half a day, and they go first because **each one gates a design choice** — getting an unexpected answer in week 3 is expensive, in week 1 it is free.*

**Rule for this document:** record the result even when it matches the assumption. "Confirmed on Tutor, <date>, Redwood" is what turns a flagged assumption in the design into a verified fact, and the design document has five places waiting for exactly that.

---

## Setup (do this once)

1. A running **Tutor** instance — local is fine.
2. A test course with, deliberately:
   - one section containing **three units**, each with **two or more leaf blocks** (an HTML block and a problem block at minimum),
   - one **video block with a transcript**,
   - at least one unit left **unpublished**, and one with **unpublished draft edits** on top of published content.
3. A scratch Python file you can run in both `lms shell` and `cms shell`.
4. A place to write results — the table at the end of this document.

The awkward course structure is the point. Every one of these tests fails to show anything interesting on a single-block course.

---

## Test 1 — What does `get_item` actually return?

**Question.** For each block type we care about, does the content read give us clean text, or a descriptor that still needs rendering?

**Why it gates a decision.** The ingestion pipeline (design §5.2) assumes it can extract text per block type. If some types return a structure needing render, that is an extra step per type, and the chunking rules in §5.5 need to know what they are chunking.

**How to test.** In `cms shell`, read one block of each type — `html`, `problem`, `video`, `vertical` — and inspect what comes back: the object type, which fields hold content, and whether the text is usable as-is or contains markup/XML that needs processing.

**Record for each type:** field name holding the content, format of that content (plain text, HTML, OLX, structured), and whether any post-processing is needed.

**What the answers mean:**

| Result | Consequence |
|---|---|
| Clean text or simple HTML | Proceed as designed. Strip tags, chunk |
| Structured/OLX needing interpretation | Add a per-type extractor. Budget half a day per additional type |
| A type returns nothing useful | Exclude it from ingestion and record it as unsupported — better than shipping silent gaps |

**Also note:** how a **video transcript** is reached. The design treats transcripts as valuable text; confirm the path from a video block to its transcript file.

---

## Test 2 — Does a Celery worker inherit the published-branch setting?

**Question.** Reads run inside `branch_setting(published_only)`. In a web request the branch partly derives from request context. **A Celery worker has no request.** Does the setting hold, or must it be pinned explicitly?

**Why it gates a decision.** This is a **correctness and safety test, not a convenience one.** Principle 3 says the tutor never learns from unpublished content. If a worker silently defaults to draft-preferred, we would index draft content and violate that guarantee without any visible error.

**How to test.**
1. Take a unit that is **published**, then make a **draft edit** to it without publishing.
2. From a Celery task (not a shell, not a request — an actual queued task), read that block inside the `published_only` context.
3. Compare what comes back against the published text and against the draft text.

**The assertion:** the worker sees the **published** text, not the draft edit.

**What the answers mean:**

| Result | Consequence |
|---|---|
| Published text returned | Assumption confirmed. We still pin explicitly, as designed — belt and braces |
| Draft text returned | **The pin is mandatory, not defensive.** Add an assertion in the ingestion path that fails loudly if the active branch is not `published_only` |
| Errors outside a request context | Note the workaround needed and add it to the adapter (§3.3), not scattered through the pipeline |

**Whatever the result, add a regression test.** This is the failure that would be invisible in a demo and serious in production.

---

## Test 3 — What does a course import actually fire?

**Question.** Importing a course: does it fire one publish event per block, a single event, or nothing at all?

**Why it gates a decision.** Design §5.4 handles all three, but they cost differently. Per-block means a thundering herd and an uncapped embedding bill. Nothing means a silently empty index. The actual behaviour decides which path runs and whether the cost ceiling is a real concern or a formality.

**How to test.**
1. Attach a listener that just logs `usage_key` and `block_type` for `XBLOCK_PUBLISHED`, `COURSE_IMPORT_COMPLETED`, and `COURSE_RERUN_COMPLETED`.
2. Export the test course, then re-import it as a new course.
3. Count and classify what arrived.
4. Repeat for a **course rerun**, which may behave differently.

**Record:** number of events, their types, and whether `COURSE_IMPORT_COMPLETED` arrives *after* any per-block events.

**What the answers mean:**

| Result | Consequence |
|---|---|
| One completion event only | Best case. Subscribe to it, run one bulk index. As designed |
| Per-block flood | **The per-course cost ceiling stops being a formality.** Add debouncing: on import, drop the per-block events and wait for the completion event |
| Nothing fires | Import is invisible. The bootstrap command (§5.1) is the *only* path, so surface it in the Studio view and document it for operators |

---

## Test 4 — Meilisearch: is there an embedder, and can a plugin get a scoped key?

**Question.** Two parts. (a) Does the deployed index have an embedder configured? (b) How does a plugin obtain a permission-scoped API key, and what are the index and field names?

**Why it gates a decision.** The lexical half of hybrid retrieval (§6.1) depends on this. The semantic half does not — so this test decides whether hybrid ships in phase 2 or gets redesigned, **not** whether the system works.

**How to test.**
1. Confirm Meilisearch is provisioned by Tutor and reachable.
2. Query the index settings and check whether any embedder is configured. *(The design's claim is that it is keyword-only, based on the platform's index config having no vector field. This is the check that turns a strong signal into a fact.)*
3. Find how the platform issues scoped keys for search — trace how the existing search UI gets its key, and whether that mechanism is reachable from a plugin.
4. Record the index name and the searchable/filterable attributes.

**What the answers mean:**

| Result | Consequence |
|---|---|
| No embedder, scoped key obtainable | As designed. Hybrid retrieval is a phase-2 addition |
| Embedder already configured | Better than expected. Our vector layer could eventually collapse into it — but **do not change the MVP**; note it for phase 2 |
| Scoped key not reachable from a plugin | Hybrid needs a different approach, or stays semantic-only. **Not a blocker** — say so plainly rather than treating it as one |

---

## Test 5 — Can a publish be scoped to exclude specific children?

**Question.** The modulestore publish API accepts a blacklist. Is that reachable from our position, and does the Studio UI behave the same way?

**Why it gates a decision.** This is what makes the *"publish only this proposal"* option possible when accepting AI content into a unit that also holds the instructor's unpublished work (§9.1). Without it, accept degrades to publish-everything-after-showing-the-list, or cancel.

**Why it is last.** It only matters when the instructor loop ships, which is **not in this release** (§1.2). Run it if time allows; it is the one test that can slip without consequence.

**How to test.**
1. Take a unit with two draft children: A (our stand-in for a proposal) and B (a stand-in for the instructor's work-in-progress).
2. Attempt a publish of the unit scoped to A only.
3. Check whether B remained unpublished.
4. Separately, publish the unit through the **Studio UI** and confirm it publishes both — this is the behaviour the whole proposal-queue design exists to avoid.

**What the answers mean:**

| Result | Consequence |
|---|---|
| Scoped publish works | The three-way accept in §9.1 is buildable as designed |
| Not usable from our position | Accept offers only publish-all-after-showing-the-list, or cancel. **The instructor is still never surprised**, which is the actual guarantee |
| Studio UI publishes the whole subtree | **Confirms the finding the design is built on.** Record it — it is the evidence behind the proposal queue |

---

## Results table

Fill this in as you go. Copy the finished version into design §3.6, replacing the open items with facts.

| # | Question | Result | Date / version | Design impact |
|---|---|---|---|---|
| 1 | `get_item` return shape per block type | | | |
| 2 | Worker inherits `published_only`? | | | |
| 3 | What a course import fires | | | |
| 4 | Meilisearch embedder + scoped key | | | |
| 5 | Scoped publish with blacklist | | | |

---

## What to do with an unexpected answer

Three of these can come back differently than assumed. None of them breaks the design, and the responses are already written down — which is the point of having run them first:

- **Test 2 returning draft content** is the only one with a safety consequence. It makes the explicit branch pin mandatory and earns a loud assertion in the ingestion path.
- **Test 3 firing per-block events** turns a cost ceiling from precaution into requirement, and adds debouncing.
- **Test 4 failing on the scoped key** removes the lexical half of hybrid retrieval. The system still works; the roadmap changes.

**If a test produces something none of these anticipated, that is the most valuable result of the week** — write it up before building anything else on top of the assumption it breaks.
