# Feature B — How Exam Prep Works

*Plain-English walkthrough: from a past-paper PDF on someone's laptop to a
student practising a question and building a revision plan.*

Traced from the code on 2026-08-20. Every file named below is real.

---

## The one-line version

> Real past papers go in. **Real** questions come out for planning, and
> **AI-written** questions come out for practice — and the student can always
> tell which is which.

That last part is the whole design. Feature B never blurs the line between what
an examiner wrote and what a model wrote.

---

## Two halves that run at different times

```
OFFLINE — an operator does this once per paper
┌──────────────────────────────────────────────────────────────┐
│  PDF  →  extract questions  →  tag to outcomes  →  load pack │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
LIVE — what a student does, any time after
┌──────────────────────────────────────────────────────────────┐
│  Study plan (real questions)   │   Practice (AI questions)   │
│  Self-mark  →  mastery  →  changes both of the above         │
└──────────────────────────────────────────────────────────────┘
```

Nothing a student does ever waits on a PDF parser or a tagging model. Those ran
weeks earlier.

---

# Part 1 — Offline: getting the paper in

### Step 1. Read the PDF

*File: `tools/extract/extract_pack.py`*

A command-line tool an operator runs once per paper:

```
python tools/extract/extract_pack.py paper.pdf --offering course-v1:X+Y+Z \
       --exam-type final --year 2024 > pack.json
```

It pulls out each question, its number, its page, and **the marks printed beside
it**.

**It is a tool, not part of the service** — deliberately. Extraction is batch
work run once. Putting a PDF parser in the request path would add weight to the
container for something no student ever calls.

**What it honestly cannot do**, stated up front because a silent failure here is
expensive:

- **Digital text only.** A scanned paper has no text layer and produces nothing
- **No layout model.** A two-column paper interleaves into nonsense
- A question number drawn as a graphic is invisible

Where it can tell, it raises a `low_confidence_flag`. Where it cannot, the
question count comes out wrong — which is exactly why the tool **prints what it
found and asks the operator to check**, instead of piping straight into the
loader.

### Step 2. Tag each question to a learning outcome

*File: `ai/clo_tagger.py`*

Each question needs to be linked to a course learning outcome (a "CLO"). A cheap
model proposes the link, offline.

Three rules make this safe:

- **Refusing is the safe answer.** Unknown id, unreadable reply, dead provider —
  all leave the question untagged. An untagged question is still practisable. A
  *wrongly* tagged one sends a student to revise the wrong topic
- **Scope is enforced, not requested.** Allowed ids come from the pack's own CLO
  list, so an id from another course cannot be accepted even if the model returns
  one
- **It is a proposal.** A human can correct it

### Step 3. Load the pack

*Endpoint: `POST /coursemate/api/packs/load`*

The checked pack is loaded into the exam-prep database.

**Loading replaces.** The old questions for that course are deleted and the new
ones inserted — so a pack is a complete statement of the bank, never a partial
patch that leaves stale questions behind.

**Where it is stored matters.** Past papers live in CourseMate's own storage,
**never** in the Open edX course package. Course exports are routinely shared
between institutions — storing an exam bank there would build a machine for
leaking one university's papers to another.

---

# Part 2 — Live: what the student does

## A. The study plan — real questions only

*File: `ai/planner.py` · Endpoint: `POST /examprep/study-plan`*

The student types a session size in **marks** — say 100 — and gets a plan.

### Why marks, not question counts

A two-hour exam is 100 marks. "Eight questions" means nothing until you know
whether they are 2-mark or 25-mark questions. Marks are the unit the student
actually budgets.

### It uses no model at all

This is arithmetic over data the service already has: the outcome list, the
student's own practice counts, and questions with the marks printed on them.

A model would add nothing a weighted split cannot do — and it would add a failure
mode. **A plan that quietly drops an outcome looks exactly like a plan that
decided to drop it.** Being deterministic also means the plan is testable.

### How the budget is split

1. **Weakest outcome first.** An outcome never practised counts as fully unknown
   — "unknown is worth resolving"
2. Each outcome gets a **share** of the marks, sized by how weak it is
3. Real past-paper questions are packed into each share
4. At most **5 outcomes** — more than that is a syllabus, and a student reading a
   syllabus is not revising

### Two pieces of honesty built into it

- **A question with no marks is never guessed at.** Charging it a default would
  over-fill the student's session — the one direction that actually hurts. It is
  excluded, and the count of excluded ones is reported
- **A short plan says it is short.** If the bank cannot fill 100 marks, the plan
  reports *"80 marks could not be filled"* rather than padding with something
  that does not fit

**Study plans contain only real past-paper questions.** Nothing AI-written ever
enters a study plan.

## B. Practice questions — AI-written, and labelled

*File: `ai/quiz_generator.py` · Endpoint: `POST /examprep/practice/stream`*

The student picks an outcome and asks for a question.

```
find a real source question   → none?  ABSTAIN
retrieve lesson context, gated → fails? ABSTAIN
generate → check → validate    → only then show it
```

### It is modelled on a real question

Every generated question starts from an actual past-paper question for that
outcome. If no past-paper question is tagged to the outcome, it **abstains** —
it will not invent one from nothing.

If the first candidate's lesson context fails the confidence gate, it tries the
next, and the next. (This fixed a real bug: one outcome always abstained because
the store returns questions in `marks DESC` order and the heaviest one — an
abstract 15-mark essay — scored **0.3458** against the 0.35 threshold. Two
sibling questions scored 0.8500 and 0.7292 and were never tried.)

### Nothing appears until it is valid

The model's text is generated, parsed and validated **before the first word
reaches the screen**. Streaming live would mean a student reading a question we
then discover is malformed — and there is no way to unsay it.

It costs one extra generation of waiting. Correctness is worth more here than in
chat, because a practice question reaches a student with no instructor checking
it.

### The label cannot be faked

The model is asked for **prose only**. The "AI-generated" label, the source
citation, the marks and the difficulty are all set by the code from the record it
actually retrieved — never by the model. A label the model could get wrong would
not be a label.

### It will not hand back a copy of a real exam question

Two checks, because they fail differently:

| Check | Catches | Threshold |
|---|---|---|
| **Word overlap** (Jaccard) | a reprint | 0.6 |
| **Meaning** (embeddings) | a reworded copy | see below |

Word overlap is blind to rewording. A paraphrase of a real exam question labelled
"AI-generated" is the same false claim to the student as a copy.

So the second check compares meaning:

| Score | What happens |
|---|---|
| **≥ 0.92** | Reject — too close. Try again |
| **0.86 – 0.92** | Uncertain. Retry once; if that was the last try, serve it |
| **< 0.86** | Accept |

**Why a band and not a line.** Calibrated on 103 labelled pairs. The classes
overlap by **0.0118** — no single threshold separates them. The worst case was
*"State what a named release is."* versus *"Give one example of a named release."*
at **0.8850** — genuinely different questions a student could answer separately.

On short factual questions, similarity confuses *topic* with *identity*. That is
a property of the task, not a tuning problem — which is why the middle band
spends a retry instead of refusing.

**If the embedding provider is down, the check does nothing.** It has its own
5-second timeout, separate from the model's. A safety check that can block
generation becomes the outage.

### The same question won't keep coming back

The student's own attempt count picks which source question leads:

```
which source leads  =  attempts  modulo  number of candidates
```

Practise more, and the seed moves. It is deterministic — same inputs, same order
— so it can be tested.

If the student explicitly asks for a difficulty level, **no rotation happens**.
They asked for that specifically.

## C. Mastery — self-marked, never a grade

*File: `xblock/tutor_block.py` → `record_attempt`*

After practising, the student marks themselves. That count is stored.

**This is the only write in all of Feature B**, and where it lives is doing real
work: it sits on the XBlock, in the platform — **not** on the agent's tool
surface. The claim *"no prompt can change what a student sees"* would end the day
a `record_mastery` tool was added. The claim is worth more than the convenience.

**The student id comes from the platform session, never from the payload.** The
browser carries mastery *out*; it does not get to say whose it is on the way
back.

**It is not a grade.** It ranks the student's own study plan and nothing else. It
never reaches the gradebook.

Mastery then feeds back into both halves:

- the **study plan** puts weak outcomes first
- **practice** rotates the seed question

---

## What is built but switched off

Be precise about these — the code is real, but a student sees nothing today.

| Feature | Status |
|---|---|
| **Answer evaluation** — compare a student's answer with the examiner's | `answer_evaluation_enabled = False`, **and** 0 of the 5 questions in the live bank have a reference answer, so it returns nothing even if switched on. By design it never says "correct" and never writes mastery |
| **The agent** (multi-step planning prose) | `agent_enabled = False` |
| **Self-serve upload** for instructors and students | Not built — extraction is an operator command |
| **OCR / scanned papers** | Not built |
| **Derived difficulty** | The extractor refuses to guess a difficulty it cannot derive, so one quality metric is honestly reported as "not run" |

---

## The safety rules that never bend

- **Study plans are past-paper only.** No AI-written question ever enters one
- **Every generated question is labelled AI-generated** and cited to the paper
  and lessons it came from
- **No source question, no practice question.** It abstains rather than invent
- **Past papers never enter the course package**, so exports cannot leak them
- **Mastery never becomes a grade**
- **Enrollment is re-checked on every request**, and a mastery snapshot minted for
  another course is discarded

---

## The numbers to remember

| | |
|---|---|
| **0.6** | word-overlap threshold — catches reprints |
| **0.86 / 0.92** | the meaning band — retry vs reject |
| **103** | labelled pairs behind that band |
| **0.0118** | how much the classes overlap — why it is a band, not a line |
| **5 s** | embedding timeout; failure is a no-op, never a block |
| **100 marks** | a two-hour session, the unit a student actually budgets |
