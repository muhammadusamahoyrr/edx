"""CourseIntelligence implementation — the security chokepoint (design §6.5).

Four things happen on **every** data access, in this order, and the order matters:

    1. resolve identity        (from the verified token)
    2. check scope             (offering the caller is actually in)
    3. filter BEFORE ranking   (§6.3 — unauthorized content is never a candidate)
    4. write an audit record   (§10.5)

Scattered across callers, a new one forgets step 3 and the failure is invisible:
results look plausible because they *are* plausible, just drawn from a wider set
than the student may see. Behind this interface it cannot be skipped.
"""

from __future__ import annotations

import logging

from coursemate_contracts.auth import StudentClaims

from ..config import settings
from ..knowledge import get_store
from ..knowledge.store import StoredChunk

log = logging.getLogger(__name__)


class AuthorizationError(RuntimeError):
    """Caller asked for an offering their token does not cover."""


class CourseIntelligenceImpl:
    """Read-only. §10.6 leans on that: the tool surface cannot change what
    students see, so no prompt can either."""

    def _authorize(self, claims: StudentClaims, offering_id: str) -> None:
        # Step 2. The token establishes WHO is asking; it is not a grant. In the
        # MVP the offering is checked against the token's own claim.
        #
        # LIMITATION, stated rather than hidden (§10.1): enrollment is not yet
        # re-derived from the platform on each call. A forged token cannot be
        # minted without the signing key, so this is not currently exploitable —
        # but it is weaker than the design requires and is tracked as such.
        if offering_id != claims.offering_id:
            raise AuthorizationError(
                f"token scoped to {claims.offering_id}, requested {offering_id}"
            )

    def _audit(self, claims: StudentClaims, tool: str, offering_id: str, n: int) -> None:
        # Step 4. Deliberately not the student's question: §3.1 keeps chat text
        # out of our logs, and an audit trail does not need the content to record
        # that access happened.
        log.info(
            "audit tool=%s user=%s offering=%s tenant=%s results=%d",
            tool, claims.sub, offering_id, settings.tenant, n,
        )

    def retrieve_course_context(
        self, query: str, offering_id: str, claims: StudentClaims, limit: int = 5
    ) -> list[StoredChunk]:
        self._authorize(claims, offering_id)
        # Step 3: tenant + offering + active are part of the SQL, so filtering
        # happens before ranking rather than after it.
        chunks = get_store().search(
            query, tenant=settings.tenant, offering_id=offering_id, limit=limit
        )
        self._audit(claims, "retrieve_course_context", offering_id, len(chunks))
        return chunks

    def has_index(self, offering_id: str) -> bool:
        return get_store().has_index(offering_id)


boundary = CourseIntelligenceImpl()
