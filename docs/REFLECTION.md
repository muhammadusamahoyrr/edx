# Reflection — what the AI did well, and where it failed

CourseMate was built with an AI coding tool (Claude Code) doing most of the
typing. This document is the honest account of that: where it helped, where it
was confidently wrong, and what caught it.

It is deliberately weighted toward the failures. A reflection that lists only
successes is a marketing document, and the failures are where the transferable
lesson is.

## How to read this

Three kinds of statement appear below, and they are not equally trustworthy. They
are labelled throughout:

| Label | Means |
|---|---|
| **AI reasoning** | A model's explanation or hypothesis. Plausible; not evidence. |
| **Human direction** | A decision, constraint, or refusal supplied by the developer. |
| **Evidence** | Something observed — a command's output, an exit code, a counter, a measured number. This is the only category that settles anything. |

**Sourcing.** Everything below is drawn from this repository: commit messages,
`docs/prompts.md` section 5, `CLAUDE.md`, `docs/LIMITATIONS.md`, and command
output recorded during the sessions that produced them. **There are no
reconstructed transcripts in this document.** Where a claim rests on a commit
message rather than a first-hand observation, it says so.

---

## What the AI did well

**Mechanical work at a scale a human would skip.** `eb8062f` cleared 84 dead
`noqa` directives. Nobody was ever going to do that by hand, and leaving them is
how a suppression outlives the problem it suppressed.

**Writing the test that proves its own fix wrong.** The most useful habit was
reverse-checking: take the new tests, run them against a *pre-fix* copy, and
require them to fail. **Evidence:** for the generator-fallback change, 4 of 6 new
tests failed without the fix and 2 passed — the 2 being exactly the ones that
assert *unchanged* behaviour. A test that has never failed proves nothing, and
this is the cheapest way to show that it can.

**Diagnosis over guessing, when pushed for evidence.** Practice generation
abstained on one learning outcome and worked on another. The intuitive answer —
"the course lacks material for that outcome" — was wrong. **Evidence:** the
retrieval gate scores the *seed question's own text*; the store orders
`marks DESC`; the heaviest question for that outcome was an abstract essay
scoring **0.3458 against a threshold of 0.35**, while two sibling questions
scored **0.8500** and **0.7292** and were never tried. The outcome was in fact
the best-covered topic in the course. Fixed in `dc15689`.

**Reading source rather than trusting names.** A partition-lookup helper was
adopted only after reading the platform's own source, which revealed that it
*persists* a group assignment. Called on every token mint, it would have enrolled
students into A/B experiment groups for opening a chat box. Caught by reading,
not by the function's name.

**Documenting the reasoning, not just the change.** Commit messages in this repo
carry why a change was made and what was rejected. That is the only reason the
development log in `docs/prompts.md` section 5 could be reconstructed at all.

---

## Where the AI failed

Three failures from a single session (2026-08-19), recorded in `CLAUDE.md` and
`docs/prompts.md` section 5. All three were confident, plausible, and wrong.

### 1. A correct observation, an inverted conclusion

**AI reasoning:** the process holding the WSL environment open was fragile
because its parent was PID 1428 "rather than PID 1", implying it was attached to
an interactive shell and would die when that shell exited.

**Evidence:** PID 1428 is WSL's own `/init` session shim, and *its* parent is
PID 1. The process also held its own session ID, so it was already detached. It
had survived dozens of separate invocations over more than two hours.

**What was actually true:** being inside a WSL session is *precisely why it
worked*. The property flagged as a weakness was the mechanism. The reasoning
started from a real observation and drew the opposite of the correct conclusion.

### 2. A fix that was enabled, active, and useless

**AI reasoning:** move the keepalive under `systemd` so it is owned by PID 1,
restarts automatically, and no longer depends on any shell.

**Human direction:** verify that it actually holds the environment open, rather
than trusting that the unit reports itself active.

**Evidence:** the unit was installed, enabled, and reported `active`. The
environment then terminated anyway — with `systemd`, Docker, containerd and all
13 containers running. `ps -o etime= -p 1` was observed **decreasing** between
two consecutive commands (`00:18`, then `00:13`), meaning the environment was
dying after each invocation and being rebooted by the next one.

**What was actually true:** WSL ends a distro based on whether a *session* is
active, not on whether processes exist inside it. A `systemd` service lives
outside any session, so it is not counted. **This is now recorded in `CLAUDE.md`
with an explicit "do not try this again" note**, because it is the kind of dead
end that looks correct on every check except the one that matters.

**The compounding cost of failure 1.** Because the parent-PID reasoning was
wrong, the working keepalive was retired in favour of the broken replacement —
briefly removing the protection altogether. A wrong diagnosis did more damage
than the original problem.

### 3. A rescue that had already expired

**AI reasoning:** before rebuilding, preserve the outgoing container image by
taking a snapshot of the running container.

**Evidence:** the command failed twice with
`NotFound: content digest sha256:8eb568a371ec…: not found`, exit code 1. The
image's tag had already been reassigned by an earlier rebuild, and containerd had
garbage-collected its content blobs.

**What was actually true:** the option was already impossible at the moment it
was proposed. The reasoning ran forward to a desired end state without checking
whether its precondition still held. **Human direction** then set the durable
rule — *tag the outgoing image before rebuilding* — which is now in `CLAUDE.md`
and was applied successfully on the next two deployments.

---

---

## A second kind of failure — the AI describing the system

The three above are the AI *building*. This one is the AI *explaining*, and it
turned out to be no more reliable.

On 2026-08-20 the model produced a slide-content document about this project:
15 slides, ~8,000 words, every figure supposedly checked. Two audit passes
found **13 errors in it**. All were first-hand; none is reconstructed.

### 4. A defect that did not exist

**AI reasoning:** a slide listing future work claimed *"one ordering-dependent
test failure remains"*.

**Evidence:** it could not be reproduced. The named test passes alone, passes in
its own file, and passes in the full suite. Nothing in the repository documents
such a failure. `make check` exits 0.

**What was actually true:** the claim was carried forward from an earlier
session's summary and never checked. It made the project look **worse** than it
is, on no evidence — which is the same failure as an unfounded success claim,
pointing the other way. It was removed rather than softened.

### 5. A false alarm produced by a broken command

While checking (4), running `pytest packages/coursemate-service/tests` reported
**8 failures**, which looked like confirmation.

**Evidence:** the Makefile runs `pytest $(TESTS) -q --asyncio-mode=auto`. The
flag had been omitted, so every async test errored. With it: **1020 passed, 3
xfailed, 0 failed.**

**What was actually true:** the tool was broken, not the code. This is the
`sha1sum` failure from the same project repeating in a new costume — a command
that fails in a way that *looks like a result*. The near-miss is the lesson: a
wrong invocation had almost been reported as a repository defect, and it was
only caught by asking why the Makefile's command differed from the one typed.

### 6. A quotation that was plausible and wrong

A slide about the **chat** confidence gate quoted the abstention message as
*"There isn't enough in this course's material to plan that reliably."*

**Evidence:** `tutor.js` holds **three** distinct abstention strings — one for
chat, one for the study planner, one for practice generation. The quoted line is
the planner's. The chat gate says *"That doesn't appear to be covered in this
course."*

**What was actually true:** the wording was recalled rather than read. It was
fluent, it was in the right register, and it belonged to a different feature —
which is precisely the shape of the confident-wrong-lesson answer the tutor
itself is built to prevent. The AI made, about its own system, the class of
error that system exists to catch.

### 7. An invented number

A speaker note read *"Thirty students asking questions would have taken the LMS
down."* No such measurement exists. The repository measures three concurrent
generations, and argues the mechanism — worker pools empty by occupancy — without
naming a breaking point. "Thirty" was fabricated to make the sentence land.

Deleted. The mechanism is the claim; a number that sounds specific and has no
run behind it is worse than no number.

### 8. Markup that was not markup, twice

`**bold**` and `*italic*` written into `python-docx` render as literal asterisks.
Found once, fixed, and then **found again** — because the first check searched
for the single string `*or*` rather than for the pattern. The second pass used a
regex and found `*plus*` on another slide.

**The lesson is about the check, not the bug.** A verification narrower than the
class of defect it is meant to catch will report success while instances remain.

---

### What this session adds to the pattern

The three 2026-08-19 failures were about **doing**. These five are about
**describing**, and they rhyme:

* A claim carried forward from a summary and never re-derived (4)
* A tool failure read as a result (5)
* A recalled string preferred over a read one (6)
* A specific-sounding number with no run behind it (7)
* A check narrower than the defect class (8)

**The useful finding is the ratio.** One document, written carefully, with the
repository open: 13 errors. The first audit found 8; the second found 5 more,
including one the first had already looked for. Two of the 13 would have been
visible to a panellist within seconds — the wrong quotation, and a claimed test
failure that does not reproduce.

Prose about a system gets no compiler, no test suite and no type checker. It is
the least verified artifact in the project and it is the one people read first.

## What measurement found that review did not

These predate the session above and are the strongest argument for how this
project was built. `docs/LIMITATIONS.md` section 9 names four:

1. **A confidence gate that could never fire.** `score = raw / best` makes the
   top hit exactly 1.0 for every query. The gate existed, had tests, and was
   structurally incapable of triggering.
2. **88% of a course silently unindexed.** A 226-block course served 26 blocks:
   each ingest batch swapped itself in and deactivated its predecessors. Nothing
   failed; the content vanished quietly.
3. **A platform-breaking settings import.**
4. **An FTS5 injection.**

**A documentation inconsistency worth naming rather than smoothing over:**
`README.md` documents **six** such defects in its "What measurement found that
review did not" table, overlapping with the list above on two. The additional
entries there are Celery discarding every task while Publish returned 200, an
enqueued reindex that activated nothing, a partition lookup that was a write
rather than a read, and a frame type the UI rendered that nothing ever emitted.
The two documents disagree on the count. That is not reconciled here, because
doing so from memory is exactly the kind of unsupported tidying this document
exists to avoid.

**The common shape, in every case: the failure path returned success.** Three
were invisible in normal use and two looked like success. That is why the probes
in `tools/verification/` assert on what a *user* would check, and why an "empty"
result in them is always disambiguated — a control that fails closed hides its
own failure.

---

## The pattern

The AI was reliably good at **doing** and unreliable at **knowing whether it had
worked**. Every failure above shares one shape: a plausible mechanism, a check
that returned success, and no measurement that could have returned failure.

What caught all of them was the same discipline:

* **Ask for a check that can fail.** "The service is running" cannot fail
  usefully; "PID 1's age increases across separate invocations" can.
* **Distrust a number until you know what produced it.** A previously recorded
  agent tool-selection figure of `0.44` was measuring *timeouts*, not tool
  choice. When that metric was measured properly later, the run was first timed
  against the timeout threshold — **99 seconds for a run that would have taken
  roughly 45 minutes had the calls been timing out** — before the resulting
  **0.78** was believed.
* **Prefer evidence a user would recognise.** Container counts and HTTP status
  codes over "the deployment script exited 0".
* **Check the class, not the instance.** Searching for the one bad string
  found one of two; searching for the pattern found both. A check narrower
  than the defect will pass while the defect remains.
* **Read the string, do not recall it.** Every quoted message, threshold and
  filename in a document is a claim, and the cost of opening the file is
  seconds.

---

## What this document does not claim

* **The citation-chip fix (`7afc011`) is now browser-verified** — corrected
  2026-08-20. This bullet previously said the opposite, and said it for a day
  after the verification became possible. Real Chrome, real clicks, deployed
  image: the paper chip is a `SPAN` with no `href` and clicking it moves neither
  scroll nor history, while the lesson chip navigates to the vertical the
  modulestore confirms owns the cited block. The mastery badge and the
  study-plan shortfall were verified in the same pass.

  The caution behind the original wording still stands, and is the reason the
  check was worth doing: harness doubles have disagreed with a real DOM in this
  project before, and the chip bug itself survived because a test counted chips
  without ever reading their `href`.
* **The agent's tool-selection accuracy of 0.78** is a single measured run,
  reproduced twice, on one model against one gold set. It is a measurement, not
  a rate.
* **Entries in `docs/prompts.md` section 5 dated 2026-08-12 to 08-18 are
  reconstructed from commit messages**, not transcripts, and are labelled as
  such there.
* **No claim is made that AI-written code was correct because its tests passed.**
  Several of the defects listed above passed their tests.
