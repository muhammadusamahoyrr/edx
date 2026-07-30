"""LMS-side receivers — design §3.4 rule 4.

These exist because **the events we depend on fire in two different processes.**
`content_authoring` events fire in Studio; `learning` events fire in the LMS. An
earlier draft of the design put every receiver in the CMS, which would have left
enrollment changes entirely unobserved — and two guarantees depend on seeing them:
the metadata cache invalidates on enrollment (§6.4), and §10.7 scopes personal
data on unenrollment.

The metadata cache is deliberately the shortest-lived tier precisely because a
*revoked* enrollment must stop working quickly. This receiver is what makes that
immediate rather than a TTL wait.
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger(__name__)


def on_unenrollment(signal, sender, enrollment, **kwargs):  # noqa: ARG001
    """COURSE_UNENROLLMENT_COMPLETED.

    Thin, like every receiver: post an invalidation notice and return. Two things
    happen service-side as a result — the permission cache entry is dropped, and
    that student's course-linked personal data is scoped or purged (§10.7).
    """
    from ..tasks.invalidate import post_invalidation

    user = getattr(enrollment, "user", None)
    course = getattr(enrollment, "course", None)

    post_invalidation.delay(
        reason="unenrolled",
        course_id=str(getattr(course, "course_key", "") or ""),
        student_id=str(getattr(user, "id", "") or ""),
        trace_id=str(uuid.uuid4()),
    )
    log.info("coursemate: enqueued unenrollment invalidation")
