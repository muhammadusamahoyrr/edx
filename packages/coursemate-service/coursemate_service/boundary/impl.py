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
from ..knowledge.rerank import get_reranker
from .authz import NotEnrolled, PlatformUnreachable, verifier
from ..knowledge.store import StoredChunk

log = logging.getLogger(__name__)


class AuthorizationError(RuntimeError):
    """Caller asked for an offering their token does not cover."""


class CourseIntelligenceImpl:
    """Read-only. §10.6 leans on that: the tool surface cannot change what
    students see, so no prompt can either."""

    def _authorize(self, claims: StudentClaims, offering_id: str) -> None:
        """Step 2, in two parts — and both are needed.

        The token scopes the request; the PLATFORM decides entitlement. Checking
        only the token would mean a signed token outlives the enrollment it was
        minted under: unenroll a student and their unexpired token keeps working.
        The signature proves the token was issued, not that access still holds.
        """
        if offering_id != claims.offering_id:
            raise AuthorizationError(
                f"token scoped to {claims.offering_id}, requested {offering_id}"
            )

        if not settings.enforce_enrollment:
            log.warning("enrollment enforcement DISABLED — development only")
            return

        try:
            # The enrollment API keys on username; sub is the numeric id.
            # Falling back to sub makes the failure a clean 'not enrolled'
            # rather than a confusing 404 against a numeric username.
            verifier.require_enrolled(claims.username or claims.sub, offering_id)
        except NotEnrolled as exc:
            raise AuthorizationError(str(exc)) from exc
        except PlatformUnreachable as exc:
            # Fail CLOSED. An availability problem must never become an
            # authorization bypass: a tutor that is down is recoverable, one
            # serving another cohort's content is not.
            log.error("enrollment unverifiable, denying: %s", exc)
            raise AuthorizationError("enrollment could not be verified") from exc

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
        #
        # Retrieve MANY, then rerank to few (§8.2). Retrieving only `limit`
        # directly would leave the reranker nothing to choose between — it can
        # reorder what BM25 returned but cannot recover a better chunk BM25
        # ranked 8th. The candidate pool is where reranking earns its keep.
        candidates = get_store().search(
            query,
            tenant=settings.tenant,
            offering_id=offering_id,
            limit=settings.retrieve_candidates,
        )
        chunks = get_reranker().rerank(query, candidates, top_k=limit)

        self._audit(claims, "retrieve_course_context", offering_id, len(chunks))
        return chunks

    def has_index(self, offering_id: str) -> bool:
        return get_store().has_index(offering_id)


boundary = CourseIntelligenceImpl()
