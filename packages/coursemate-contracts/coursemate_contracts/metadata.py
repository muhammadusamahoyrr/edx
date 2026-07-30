"""The retrieval metadata schema — design §6.2.

Shared rather than duplicated because retrieval filters on these fields *before*
ranking (§6.3, §10.2). A field name that drifts between the writer and the reader
does not raise; it silently stops filtering, which is how isolation fails quietly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ContentType(StrEnum):
    LESSON = "lesson"
    PROBLEM = "problem"
    TRANSCRIPT = "transcript"
    SLIDE = "slide"
    PAST_PAPER = "past_paper"
    CLO_DOC = "clo_doc"
    STUDENT_NOTE = "student_note"


#: Content types that may only ever appear in a per-student namespace (§6.3).
PERSONAL_CONTENT_TYPES: frozenset[ContentType] = frozenset(
    {ContentType.STUDENT_NOTE}
)


class ChunkMetadata(BaseModel):
    """One indexed chunk's filterable metadata."""

    # --- identity -------------------------------------------------------------
    #: Single-valued in the MVP (§3.5), but present from day one: retrofitting an
    #: isolation key later is expensive, carrying an unused one costs nothing.
    tenant: str
    course_id: str
    #: The real isolation unit — see §6.5. CS-101 Fall 2026 != CS-101 Fall 2027.
    offering_id: str
    usage_key: str
    block_id: str
    block_type: str

    # --- versioning (§5.3 write-then-swap) ------------------------------------
    version: str
    #: The swap pointer. Retrieval always filters on active=True, so stale and
    #: current content can never coexist.
    active: bool = True
    course_version: str | None = None
    #: About the *content* — when the instructor published it.
    publish_time: datetime | None = None
    #: About *our pipeline* — when we last wrote it. Not the same thing, and
    #: debugging a stale answer needs both (§6.2).
    updated_at: datetime | None = None

    # --- nature ---------------------------------------------------------------
    content_type: ContentType
    language: str = "en"

    # --- targeting ------------------------------------------------------------
    clo: str | None = None
    week: int | None = Field(default=None, ge=0)
    topic: str | None = None

    # --- isolation ------------------------------------------------------------
    #: Set only for personal uploads. Its presence makes a chunk uncacheable
    #: (§6.4, §10.2) — see coursemate_service.knowledge.cache.policy.
    student: str | None = None

    @property
    def is_personal(self) -> bool:
        return self.student is not None
