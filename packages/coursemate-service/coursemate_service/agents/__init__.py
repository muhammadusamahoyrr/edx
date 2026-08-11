"""The exam-prep agent — design §6.5, §7, §9.0.

An agent, and not a pipeline, because the task is genuinely open-ended: "help me
revise for the final" has no fixed sequence of steps. Which CLOs matter depends on
the student's mastery; which past questions to pull depends on which CLOs; whether
course content is needed at all depends on what the questions turn out to cover.
A hardcoded chain would have to guess all of that in advance.

**What this package may not do**, enforced rather than reviewed:

* It never touches `coursemate_service.knowledge` (`.importlinter` contract 3,
  which was widened from `ai` to cover `agents` in the same commit that created
  this directory). Everything goes through the `CourseIntelligence` boundary, so
  authorize → filter → audit happens on every access and a new tool cannot forget.
* It never imports the dormant proposal queue (contract 4, widened likewise).
* Its entire tool surface is read-only. §10.6 — "there is no prompt that makes
  CourseMate change what students see" — is a structural claim, and this is the
  structure. Mastery writes are platform-side, off this surface entirely.

**It ships dark.** `settings.agent_enabled` defaults to `False` and is read at the
API layer, so a default install routes exam prep to the deterministic path and no
code in this package runs at all.
"""

from __future__ import annotations
