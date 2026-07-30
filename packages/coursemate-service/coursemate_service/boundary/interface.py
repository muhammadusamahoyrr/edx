"""The Course-Intelligence boundary — design §6.5.

Four read-only tools. The design answers the obvious objection ("if your only
consumer is LangGraph, why not call retrieval directly?") with security rather
than extensibility:

    Four things must happen on *every* data access: resolve identity, check
    enrollment/role for the requested scope, apply the tenant/student filter
    BEFORE ranking, write an audit record. Scattered across agent nodes, a new
    node can forget one. Behind a single interface they are a chokepoint that
    cannot be bypassed.

That argument holds with exactly one consumer, which is why this exists on day
one. `.importlinter` contract 3 forbids `agents` from importing `knowledge`, so
the chokepoint is enforced by CI rather than by review.

Every tool is keyed on **offering_id**, never course_id: the offering is the real
isolation unit, since CS-101 Fall 2026 holds a different exam-prep pack and a
different cohort from the same course run a year later. The boundary resolves
course_id -> offering_id from the caller's enrollment.

Promotion trigger, written down: expose this same contract over MCP when a second
consumer appears. The signatures, authz checks, audit records and return schemas
already *are* the MCP contract; promotion is wiring, not rework.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from coursemate_contracts.examprep import ExamPrepPack
from coursemate_contracts.metadata import ChunkMetadata
from pydantic import BaseModel


class RetrievedChunk(BaseModel):
    text: str
    metadata: ChunkMetadata
    score: float


class Progress(BaseModel):
    offering_id: str
    completed_usage_keys: list[str] = []
    percent_complete: float = 0.0


class StruggleSignal(BaseModel):
    """Aggregate and anonymised, with a k-anonymity floor (§10.3).

    Below k=5 distinct students the signal is **suppressed entirely** rather than
    rounded or bucketed — "2 of 4 students are stuck on X" identifies people, and
    small cohorts are common in university courses.
    """

    topic: str
    distinct_students: int
    fraction_struggling: float


@runtime_checkable
class CourseIntelligence(Protocol):
    """The only path from reasoning to knowledge. Read-only, by design.

    §10.6 leans on that: "the agent's entire tool surface is read-only, and the
    only path into a course runs through the proposal queue and a human accept.
    There is no prompt that makes CourseMate change what students see."
    """

    async def retrieve_course_context(
        self, query: str, offering_id: str, student_id: str
    ) -> list[RetrievedChunk]:
        ...

    async def get_student_progress(self, student_id: str, offering_id: str) -> Progress:
        ...

    async def get_struggle_signals(self, offering_id: str) -> list[StruggleSignal]:
        """Deferred with the instructor loop (§1.2, §9.3), and the k=5 floor is
        deferred *with* it — a control may be deferred together with the feature
        it guards, never while shipping the thing it protects."""
        ...

    async def get_exam_prep_pack(self, offering_id: str, student_id: str) -> ExamPrepPack:
        ...
