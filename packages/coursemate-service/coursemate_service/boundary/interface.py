"""The Course-Intelligence boundary contract — design §6.5.

The design's argument for a boundary is security, not extensibility:

    Four things must happen on *every* data access: resolve identity, check
    enrollment/role for the requested scope, apply the tenant/student filter
    BEFORE ranking, write an audit record. Scattered across agent nodes, a new
    node can forget one. Behind a single interface they are a chokepoint that
    cannot be bypassed.

That argument holds with exactly one consumer, which is why this exists now.
`.importlinter` contract 3 forbids `ai` and `agents` from importing `knowledge`,
so the chokepoint is enforced by CI rather than by review.

**This file states only what is implemented.** An earlier version declared four
tools while `CourseIntelligenceImpl` provided one — a documented contract that
did not match the code, which is worse than no contract at all: it invites a
caller to depend on a method that does not exist. The deferred tools are listed
at the bottom as prose, not as a Protocol anything can be type-checked against.

Every tool is keyed on **offering_id**, never course_id: the offering is the real
isolation unit, since CS-101 Fall 2026 holds a different cohort from the same
course a year later.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.examprep import CLO, QuestionRecord

from ..knowledge.store import StoredChunk


@runtime_checkable
class CourseIntelligence(Protocol):
    """The only path from reasoning to knowledge. Read-only, by design.

    §10.6 leans on that read-only surface: *"the agent's entire tool surface is
    read-only, and the only path into a course runs through the proposal queue
    and a human accept. There is no prompt that makes CourseMate change what
    students see."*
    """

    def retrieve_course_context(
        self, query: str, offering_id: str, claims: StudentClaims, limit: int = 5
    ) -> list[StoredChunk]:
        """Scoped, filtered, reranked and audited course content."""
        ...

    def has_index(self, offering_id: str) -> bool:
        """Whether this offering has been indexed at all.

        Separate from "nothing matched" on purpose: they produce different
        messages to the student, and only one of them means "come back later"
        (§5.1).
        """
        ...

    # --- Feature B (§7) ---------------------------------------------------

    def search_past_questions(
        self,
        offering_id: str,
        claims: StudentClaims,
        *,
        query: str | None = None,
        clo_id: str | list[str] | None = None,
        exam_type: str | None = None,
        year_from: int | None = None,
        min_marks: int | None = None,
        limit: int = 10,
    ) -> list[QuestionRecord]:
        """Structured search over past papers — a metadata filter, not a blob
        search (§7.6). Keyword-only past `claims` so a caller cannot transpose
        `clo_id` and `exam_type` positionally and get a plausible wrong answer."""
        ...

    def get_clos(self, offering_id: str, claims: StudentClaims) -> list[CLO]:
        """The confirmed course learning outcomes (§7.3)."""
        ...

    def has_exam_pack(self, offering_id: str) -> bool:
        """Whether past papers were loaded. Pairs with `has_index` (§5.1)."""
        ...


# --- Deferred tools -------------------------------------------------------
#
# Specified in design §6.5 and deliberately NOT declared above, because nothing
# implements them yet:
#
#   get_student_progress(student_id, offering_id)
#       Completion/Grades APIs. Lands with progress-aware tutoring.
#
#   get_struggle_signals(offering_id)
#       Aggregate, anonymised, k-anonymity floor of 5 distinct students (§10.3).
#       Deferred WITH the instructor loop (§1.2, §9.3) — a control may be
#       deferred together with the feature it guards, never while shipping the
#       thing it protects. §9.3 also notes the signal would be biased in the MVP:
#       the tutor is placed per-unit by an instructor, so "students are stuck on
#       X" can only surface where a tutor was already added.
#
# --- Deliberately NOT on this interface -----------------------------------
#
#   get_mastery / record_attempt
#       Mastery is the agent's memory layer, and it is reachable without being
#       here. READS arrive in the request payload, carried by the browser from
#       `Scope.user_state` — the same courier §3.1 settled for chat history, for
#       the same reason: a second copy of per-student data in the reasoning
#       service would put it outside platform user-retirement's reach.
#
#       WRITES are platform-side, through the XBlock handler, and that is the
#       load-bearing part. Putting a write on this interface would end the
#       read-only tool surface, and §10.6's claim — "there is no prompt that
#       makes CourseMate change what students see" — rests on it. The claim is
#       worth more than the convenience.
