"""Cache invalidation from the LMS (design §3.4 hop 2, §6.4).

Carries the service credential. Exists so a revoked enrollment stops working
**immediately** rather than waiting out the authz cache TTL — the TTL bounds the
worst case, this removes it in the common one.
"""

from __future__ import annotations

import logging

from coursemate_contracts.invalidation import InvalidationNotice, InvalidationReason
from fastapi import APIRouter, Depends

from ..boundary.authz import verifier
from .deps import contract_version_guard, service_credential

log = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(service_credential), Depends(contract_version_guard)]
)


@router.post("")
async def invalidate(notice: InvalidationNotice) -> dict:
    dropped = verifier.invalidate(
        user_id=notice.student_id or None,
        offering_id=notice.offering_id or None,
    )
    log.info(
        "invalidation reason=%s user=%s offering=%s dropped=%d",
        notice.reason, notice.student_id, notice.offering_id, dropped,
    )
    # Unenrollment also scopes down that student's personal data (§10.7). The
    # deletion path is built; wiring it here lands with personal uploads.
    if notice.reason == InvalidationReason.UNENROLLED:
        log.info("unenrollment: personal-data scoping deferred with uploads (§7.2)")
    return {"dropped_entitlements": dropped}
