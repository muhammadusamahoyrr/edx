# The problem CourseMate solves

Online courses reach millions, but the learning experience is thin in ways that
are well documented. CourseMate does not attempt all of them. It attacks one —
and the choice of which one is the design decision the rest of the system follows
from.

## The problem

**Learners get stuck outside working hours.** Most online study happens when no
instructor is online. Forum replies take days, so a single sticking point stalls
a lesson or ends a study session entirely. The help has to be available at the
moment of friction, in the lesson, or it is not help.

**A general chatbot is worse than no chatbot.** Point a learner at a general
assistant and it answers from the whole internet: different notation, conventions
that contradict the syllabus, and confident invention where it has no knowledge.
In a course, a plausible wrong answer is not a neutral failure — it actively
teaches the wrong thing, and the learner has no way to tell. Worse, it will
happily explain material the instructor has not published yet, or that this
learner is not enrolled to see.

**So the hard problem is not answering. It is trustworthy answering.** A tutor
embedded in a course must be able to say, for every sentence it produces, *which
piece of this course that came from* — and must be willing to say "this course
does not cover that" rather than fill the gap. That requires the AI to be bound
to the course's own published content, to cite it, to abstain outside it, and to
respect who is allowed to see what. None of those are prompt-engineering
problems; they are systems problems.

**And it must not damage the platform it lives in.** A tutor that hangs the
Publish button, occupies web workers while a model thinks, or takes the LMS down
when a model provider has an outage has traded one problem for a worse one.

## What CourseMate does about it

- **Answers inside the lesson, at any hour**, as an XBlock the instructor drops
  into a unit.
- **Answers only from published course content.** Drafts are never indexed;
  unpublished content is removed by a reconciliation sweep, because Open edX
  emits no unpublish event and nothing else would catch it.
- **Cites every claim** back to the specific block it came from, as a link the
  learner can follow into the course.
- **Abstains** when the course does not cover the question, instead of answering
  from general knowledge.
- **Enforces enrollment** on every call, re-derived against Open edX and failing
  closed, so unauthorised content is never even a retrieval candidate.
- **Never occupies an LMS worker.** The browser streams directly from a separate
  service over a same-origin path; the XBlock only mints a short-lived token.
  Model latency and model outages cannot reach the platform.

These claims are measured, not asserted — retrieval quality, groundedness,
citation correctness, false-answer and false-abstention rates, latency, and
authorization correctness all have reproducible benchmarks. See
`docs/BENCHMARKS.md`, and `docs/LIMITATIONS.md` for what the numbers do **not**
yet establish.

## What CourseMate deliberately does not solve

Stated here rather than discovered later. Each is designed and scoped; none is
built.

- **Instructor analytics.** Teachers still find out who struggled after the quiz.
  Aggregating sticking points across a cohort and surfacing them in time to
  intervene is a separate product with its own privacy design. The proposal
  queue schema exists and is dormant.
- **Exam preparation.** Linking scattered PDFs and past papers back to a course's
  learning outcomes, so a learner can tell what they have mastered, is Feature B.
  Contracts only — no extraction, no outcome tagging, no interface.
- **Personalisation.** CourseMate scales *answering* to any number of learners,
  which is a real result of the architecture. It does not model an individual
  learner: nothing reads completion, grades, or mastery, and memory is the
  conversation within a block.
- **Learning outcomes.** A Socratic mode exists and preserves grounding — the
  guiding question is itself derived from retrieved content — and instructors can
  enable it per block. But nothing here measures whether it improves
  understanding, and completion rates are not tracked. Any claim that CourseMate
  raises completion or deepens learning would be a hypothesis, not a finding.

The scope is narrow on purpose. A course tutor that is *trusted* is a
precondition for everything in this list; a tutor that invents an answer once has
lost the learner for the features that would have come after.
