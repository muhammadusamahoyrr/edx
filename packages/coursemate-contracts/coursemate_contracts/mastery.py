"""Mastery — the agent's memory layer, and where it is allowed to live.

**Mastery is platform-owned, carried by the browser, and never stored by the
service.** That is the same rule §3.1 already settled for chat history, applied to
the same kind of data for the same reasons: it is per-student, it is PII-adjacent,
and platform user-retirement has to be able to delete it. A second copy in the
reasoning service would make "the platform keeps each student's data" false.

Two storage options were rejected, and the reasons are worth keeping:

* **Redis.** Measured on this deployment: `maxmemory-policy allkeys-lru`,
  `maxmemory 4 GB`, `appendonly yes`, shared with Celery's broker. `allkeys-lru`
  evicts keys *that have no TTL* under memory pressure, AOF then persists the
  eviction, and nothing logs it. Mastery is durable learning state with no TTL,
  so on this configuration it can silently disappear — and the symptom would be
  recommendations quietly getting worse, which is exactly the invisible-failure
  shape this project keeps finding. Redis may cache mastery; it may not own it.
* **The service's own SQLite.** Durable, but it is the second-PII-store that
  §3.1 rejected by name.

So: the Django DB on the platform owns it, the XBlock writes it, the browser
carries a snapshot to the service, and the service forgets it after the turn.

**What that costs, stated plainly.** A snapshot arriving in the request payload is
attacker-controlled in the same sense the chat history is. A student can forge
their own mastery. What that buys them is worse study recommendations for
themselves: mastery shapes which practice questions *they* are shown, it is not a
grade, it never reaches another student, and it cannot widen retrieval scope —
enrollment and offering are still re-derived at the boundary on every call. It
must therefore never be read as an assessment record, and `MasterySnapshot` is
deliberately not the type anything grade-shaped would accept.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field


class CLOMastery(BaseModel):
    """Counters for one course learning outcome.

    Counters rather than a score. Turning `(attempts, correct)` into a single
    number is a calibration question — spaced repetition, decay, difficulty
    weighting — and picking a formula now would bake an unvalidated one into
    stored data. Counters can be rescored later; a stored score cannot be
    un-rounded.
    """

    clo_id: str
    #: `easy` | `medium` | `hard`, from `examprep.band_of()`. `None` means the
    #: row aggregates every band — what `by_clo()` returns, and what a pre-band
    #: snapshot looked like.
    #:
    #: Tracked per band because "struggling with CLO-3" is not one fact: a
    #: student can be solid on the easy items and lost on the hard ones, and a
    #: single counter averages that into a number that recommends neither.
    difficulty_band: str | None = None
    attempts: int = Field(default=0, ge=0)
    correct: int = Field(default=0, ge=0)

    @property
    def accuracy(self) -> float | None:
        """None, not 0.0, when untried.

        A CLO with no attempts is *unknown*, and 0.0 would rank it identically to
        one the student has failed six times — which is the opposite of the right
        recommendation.
        """
        return (self.correct / self.attempts) if self.attempts else None


class MasterySnapshot(BaseModel):
    """What the browser carries into one exam-prep request.

    Bounded like `ChatRequest.history` and for the same reason: an unbounded
    payload is a denial-of-service the student pays for. A course with more than
    64 CLOs is outside the MVP, and truncation is visible in `truncated`.
    """

    offering_id: str
    #: Up to 64 outcomes x 3 bands. The cap is what rejects an over-long
    #: payload, and the XBlock trims to match it.
    clos: list[CLOMastery] = Field(default_factory=list, max_length=192)
    #: Set by the XBlock when the student has more CLOs than fit. The agent is
    #: told, so "no history for that CLO" is not confused with "trimmed away".
    truncated: bool = False

    def by_clo(self) -> dict[str, CLOMastery]:
        """One row per outcome, **summed across bands**.

        Callers that rank whole outcomes — the deterministic planner — want the
        aggregate, and summing here keeps them working unchanged now that rows
        are per band. Use `by_clo_band()` for the detail.
        """
        out: dict[str, CLOMastery] = {}
        for c in self.clos:
            prev = out.get(c.clo_id)
            out[c.clo_id] = CLOMastery(
                clo_id=c.clo_id,
                difficulty_band=None,
                attempts=(prev.attempts if prev else 0) + c.attempts,
                correct=(prev.correct if prev else 0) + c.correct,
            )
        return out

    def by_clo_band(self) -> dict[tuple[str, str | None], CLOMastery]:
        """Per `(outcome, band)` — what the generator uses to pick a band."""
        return {(c.clo_id, c.difficulty_band): c for c in self.clos}


class AttemptOutcome(BaseModel):
    """Browser -> XBlock `json_handler`, after a practice answer is submitted.

    The service never writes this. The platform does — see the module docstring.
    """

    offering_id: str
    clo_id: str
    #: Derived from the question's difficulty by `examprep.band_of()`. `None`
    #: when the question carries no difficulty estimate, which a fresh pack
    #: often does — an unbanded row is honest, a defaulted one is not.
    difficulty_band: str | None = None
    question_id: str
    attempt_id: str
    correct: bool


def idempotency_key(
    *, offering_id: str, student_id: str, clo_id: str, question_id: str, attempt_id: str
) -> str:
    """Stable key for one attempt.

    Needed twice over: a student double-clicks Submit, and the agent loop may
    retry a tool call. Both must count once.

    Fields are joined with a separator that cannot appear in an Open edX usage
    key or username, so `("a|b", "c")` and `("a", "b|c")` cannot collide into the
    same digest — the classic concatenation bug, which here would silently merge
    two students' attempts.
    """
    parts = (offering_id, student_id, clo_id, question_id, attempt_id)
    if any("\x1f" in p for p in parts):
        raise ValueError("idempotency key component contains the field separator")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
