"""Cache invalidation notices — LMS receiver to service (§3.4 hop 2).

These ride the **service credential**, not the student one. They come from
`learning` events, which fire in the LMS (§3.4 rule 4) — an earlier draft of the
design put every receiver in the CMS and would have left enrollment changes
unobserved entirely.

The metadata cache is deliberately the shortest-lived tier (§6.4) because a
*revoked* enrollment must stop working quickly. This notice makes it immediate
rather than waiting out the TTL.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class InvalidationReason(StrEnum):
    ENROLLMENT_CHANGED = "enrollment_changed"
    #: Also triggers scoping/purge of that student's course-linked personal data
    #: (§10.7).
    UNENROLLED = "unenrolled"
    CLO_EDITED = "clo_edited"
    COURSE_VERSION_BUMPED = "course_version_bumped"


class InvalidationNotice(BaseModel):
    tenant: str
    reason: InvalidationReason
    course_id: str | None = None
    offering_id: str | None = None
    student_id: str | None = None
    trace_id: str
