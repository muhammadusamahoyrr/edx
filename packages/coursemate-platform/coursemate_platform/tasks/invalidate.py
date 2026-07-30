"""Cache invalidation notices to the service (design Â§3.4 hop 2)."""

from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def post_invalidation(self, reason: str, course_id: str, student_id: str, trace_id: str):
    """Thin: forward and return.

    Uses the SERVICE credential, not a student token â€” a leaked student-path
    token must not be able to invalidate or write anything (Â§3.4).
    """
    from django.conf import settings

    from ..client.endpoints import send_invalidation

    send_invalidation(
        tenant=settings.COURSEMATE_TENANT,
        reason=reason,
        course_id=course_id or None,
        offering_id=course_id or None,
        student_id=student_id or None,
        trace_id=trace_id,
    )
    return {"reason": reason}
