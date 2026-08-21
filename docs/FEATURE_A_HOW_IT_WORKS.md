# Feature A — How the AI Course Tutor Works

*Plain-English walkthrough of what happens between a student typing a question
and an answer appearing on screen.*

Traced from the code on 2026-08-20, not from memory. The main path is
`ai/pipeline.py`; every file named below is real.

---

## The one-line version

> The student asks. We search **their course**, score how well the result
> matches, and **only then** decide whether the model is allowed to answer.

The model is the last step, not the first. Everything interesting happens before
it.

---

## The whole flow

```
Student types a question
        │
   1 ── XBlock mints a token          (0.115 ms, then the LMS is free)
        │
   2 ── Browser calls the service     (direct, not through the LMS)
        │
   3 ── Work out what to search for   (follow-ups need the previous turn)
        │
   4 ── Search the course             (BM25 over this course only)
        │
   5 ── Re-rank the results           (coverage + proximity + title)
        │
   6 ── Already answered before?      (cache — first turns only)
        │
   7 ── THE GATE  ── score < 0.35 ──► ABSTAIN. No model call. 3 ms. Done.
        │
        ▼ score high enough
   8 ── Daily spend check
        │
   9 ── Build the prompt              (retrieved text + conversation)
        │
  10 ── Stream the answer             (token by token, live)
        │
  11 ── Check every sentence          (flag what the material doesn't back)
        │
  12 ── Attach citations              (only chunks that actually contributed)
```

---

## Step by step

### 1. The XBlock hands over a token — and steps aside

The tutor is an **XBlock**: a block an instructor drops into a unit. When the
student opens it, the block does one job — it creates a short-lived signed token
(a JWT) that says *this student, this course, for the next few minutes*.

Then it stops. Measured at **0.115 ms**.

**Why this matters.** The obvious design is to let the LMS carry the
conversation. That would hold one Open edX web worker open for the whole answer.
Thirty students asking at once would exhaust the worker pool while the CPU sat
idle. A worker pool runs out because workers are **occupied**, not because they
are busy.

Measured: 3 answers generating at once produced **0 LMS log lines** and 103 ms of
LMS CPU, against a 118 ms idle baseline.

### 2. The browser talks to the service directly

The browser streams from the CourseMate service over a same-origin path, routed
by Caddy. The answer never passes through an LMS process.

So a slow model, or a model provider outage, **cannot slow down Open edX**.

### 3. Work out what to search for

*File: `ai/query.py`*

What the student typed is not always what we should search for.

> Student: *"What is a cohort?"*
> Student: *"Why would I use one?"*

Searching for *"why would I use one?"* finds nothing — "one" means nothing on its
own. So when a question contains a pro-form (*it, one, this, them…*), the
previous turn is prepended before searching.

**Only pronouns**, deliberately. That is a fixed, finite list of words, so it
cannot quietly grow into a lookup table tuned for the test set.

| | recall@3 | answered from the wrong lesson |
|---|---|---|
| Before | 0.333 | 7 of 12 |
| After | **0.917** | **1 of 12** |

Single-turn questions were unchanged — that was checked, because a fix that
improves one case and breaks another is not a fix.

**No model is used to rewrite the question.** The standard approach asks an LLM
to rewrite follow-ups. That puts a model call *before* the gate and destroys the
thing this system is built on: refusing costs nothing. On the local model it
would have added ~25 seconds to every refusal.

### 4. Search the course

*File: `knowledge/store.py`*

Lessons are split into chunks and indexed with **SQLite FTS5 / BM25** — full-text
search built into Python's standard library.

**Why not a vector database?** At 286 chunks it would be an extra container, an
extra client, and an extra way to fail, for nothing measurable. BM25 is also
**deterministic** — the same query returns the same ranked list every time, so
retrieval quality can be asserted in a test instead of eyeballed.

The honest limitation, measured and written down: lexical search misses
paraphrases. Ask *"what causes processes to hang forever?"* and it will not find
a lesson that only says "deadlock".

Three protections are inside the SQL itself, not applied afterwards:

- only **this** course
- only the **active** index version
- only content **this student** may see

Unauthorised content is never even a candidate.

### 5. Re-rank

*File: `knowledge/rerank.py`*

BM25 orders well but over-rewards one rare word appearing many times. So the top
20 candidates are re-scored on three signals:

| Signal | Weight | What it asks |
|---|---|---|
| **coverage** | 0.60 | How much of the question is actually present? |
| **proximity** | 0.15 | Do the question's words appear near each other? |
| **title** | 0.25 | Does the lesson title match? |

Then the best 3 go forward.

> **If you grep `config.py` you will see `rerank_top_k = 5`.** The live
> deployment overrides it with `COURSEMATE_RERANK_TOP_K=3`. Three is what
> actually runs; five is the code default.

Measured: the correct lesson moved from typically rank 2 to rank 1 — **MRR 0.644
→ 0.833** — for **+0.27 ms**.

### 6. Have we answered this before?

*File: `response_cache.py`*

First questions in a conversation are cacheable. A repeat gets the stored answer
instead of paying for the model again. Follow-ups are never cached — they depend
on the conversation.

### 7. The confidence gate — the heart of Feature A

*File: `ai/gate.py`*

The blended score from step 5 is compared with **0.35**. Below it, the tutor
stops and says:

> *"That doesn't appear to be covered in this course."*

**No model call. No spend. About 3 milliseconds.**

**What 0.35 actually means.** It is measured against the *blended* score, not raw
word coverage. Blended is roughly 0.855 × coverage on average, so 0.35 is a
coverage bar of about **0.41** — not "35% of the words matched".

**How it was chosen.** After the gate was fixed, covered questions scored 1.000
and uncovered ones 0.200–0.250, so 0.35 sits cleanly between the two groups.
Then it was tuned: at 0.30 a false answer appears, at 0.40 a correct answer is
lost. That is 28 questions, one course, one rater — a starting point with an
interval, not a settled number.

**The bug worth telling.** The gate originally scored `raw / best`, which makes
the top hit exactly 1.0 for *every* query. The gate existed, had tests, and
**could never fire**. It was found by measuring, not by review.

| | Before | After |
|---|---|---|
| False-answer rate | 1.000 | **0.000** |
| Correct abstentions | 0 of 6 | **3 of 3** |

**What the gate does not catch.** A *confident* match on the *wrong* lesson. Two
of ten paraphrased questions scored 0.386 and 0.475 — above the line — and were
answered from a lesson that did not address the question. That is the largest
known weakness, and it is why hybrid retrieval needs the gate recalibrated rather
than just vectors bolted on.

### 8. Daily spend check

*File: `budget.py`*

Each student has a token budget per course per day (100,000). Measured usage is
660–1,295 tokens per answer, so roughly **75–150 answers a day**.

If Redis is down, counting degrades to per-process rather than failing open or
closed — a cache outage should not become a total tutor outage.

### 9. Build the prompt

*File: `ai/prompts.py`*

The retrieved text plus the conversation. The instruction is to answer **from the
supplied material only**.

Optional **Socratic mode**, per block: the tutor opens with one guiding question
instead of the answer — still built from retrieved content, so it cannot wander
off-syllabus.

### 10. Stream the answer

*File: `ai/client.py`*

**LiteLLM Router** with two models:

| Role | Model |
|---|---|
| strong | `llama-3.3-70b-instruct` (hosted) |
| cheap | `qwen2.5:7b` (local, CPU) |

If one fails, the router moves to the other and puts the failed one in cooldown.
Text streams to the browser as it is produced, so the student sees words
immediately.

### 11. Check every sentence

*File: `ai/verify.py`*

Each sentence of the finished answer is compared with the retrieved text.
Sentences the material does not support are **flagged in the answer**, never
silently deleted or rewritten.

### 12. Attach citations

Only the chunks that **actually contributed** are cited.

This matters more than it sounds. Before, a citation was emitted for every
retrieved chunk — so a citation meant *"we searched this"*, not *"the answer used
this"*. Three authoritative-looking links under a sentence none of them
supported. Now they narrow to the ones that did.

A lesson citation is a link into the course. A past-paper citation is not a link,
because a PDF has nowhere to jump to — verified in a real browser, because the
original bug survived a test that counted the chips without ever reading them.

---

## What protects the student, at every step

| Protection | Where |
|---|---|
| Enrollment re-checked on **every** request, failing closed | Boundary, before retrieval |
| Staff-only content never indexed | Ingest time |
| Cohort and paid-track content filtered | Inside the SQL |
| Scope comes from the **token**, never the request body | Boundary |
| Every access logged — **without** the student's question text | `boundary/impl.py` |
| Search-injection blocked (typing `OR` cannot crash it) | `knowledge/store.py` |
| No `innerHTML`; `javascript:` links neutralised | `tutor.js` |

---

## What Feature A does not do

- **It does not learn from drafts.** Only published content is indexed, with a
  nightly sweep to catch anything unpublished.
- **It does not write anything to the course.** It answers; it never authors.
- **It does not know the student.** Chat memory is the conversation inside that
  block, nothing more.
- **It is not proven to improve learning.** Nothing here measures understanding
  or completion. That would need a study that was not run.

---

## The five numbers to remember

| | |
|---|---|
| **0.35** | the confidence gate |
| **3 ms** | cost of refusing — no model call, no spend |
| **0.644 → 0.833** | MRR, after re-ranking |
| **0.333 → 0.917** | follow-up questions, after the query fix |
| **1.000 → 0.000** | false-answer rate, after the gate fix |
