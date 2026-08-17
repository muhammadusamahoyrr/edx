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
from coursemate_contracts.examprep import CLO, QuestionRecord

from ..config import settings
from ..knowledge import get_examprep_store, get_store
from ..knowledge.rerank import get_reranker
from ..knowledge.store import StoredChunk
from .authz import NotEnrolled, PlatformUnreachable, verifier

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
            # Block-level access, filtered in the same query as tenant and
            # offering. A caller with no resolved groups sees unrestricted
            # content only — the fail-closed reading, matching _authorize above.
            group_tokens=frozenset(claims.group_tokens or ()),
            limit=settings.retrieve_candidates,
        )
        chunks = get_reranker().rerank(query, candidates, top_k=limit)

        self._audit(claims, "retrieve_course_context", offering_id, len(chunks))
        return chunks

    def course_summary_blocks(
        self, offering_id: str, claims: StudentClaims
    ) -> list[StoredChunk]:
        """Author-written overview blocks — same chokepoint, no ranking.

        Step 2 (authorize) and step 4 (audit) are identical to
        `retrieve_course_context` and are here rather than at the caller for the
        reason §6.5 gives: a second path that authorized itself would be a second
        place to forget. Step 3 is inside `summary_blocks`, which shares
        `search()`'s access SQL rather than restating it.

        Step 1's absence is the only difference and it is not a relaxation: there
        is no query, so there is nothing to resolve against the index.
        """
        self._authorize(claims, offering_id)

        blocks = get_store().summary_blocks(
            offering_id,
            tenant=settings.tenant,
            group_tokens=frozenset(claims.group_tokens or ()),
        )

        self._audit(claims, "course_summary_blocks", offering_id, len(blocks))
        return blocks

    def has_index(self, offering_id: str) -> bool:
        return get_store().has_index(offering_id)

    def index_version(self, offering_id: str) -> str | None:
        """Unauthenticated, matching `has_index`: it reveals only that an
        offering has been indexed and an opaque version string, never content."""
        version = get_store().stats(offering_id).get("active_version")
        return str(version) if version else None

    # --- Feature B: exam prep (§7) ---------------------------------------
    #
    # These are new tools on the SAME chokepoint, deliberately. §6.5's argument
    # for a boundary is that "scattered across agent nodes, a new node can forget
    # one" of the four steps — and an exam-prep agent is precisely the new node
    # that argument predicted. So authorize/audit run here, once, and the agent's
    # tool handlers cannot reach the store any other way (`.importlinter`
    # contract 3 now covers `coursemate_service.agents` as well as `ai`).
    #
    # Read-only, like everything else on this interface. §10.6's claim that "there
    # is no prompt that makes CourseMate change what students see" holds only
    # while that stays true, which is why mastery *writes* are not here: they
    # happen platform-side, through the XBlock, off the tool surface entirely.

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
        """Structured search over past-paper questions (§7.6).

        Step 3 — filter before ranking — is satisfied by `tenant` and
        `offering_id` being part of every WHERE clause in the store, exactly as
        they are for chunks. There is no block-level access filter here because a
        past-paper pack is offering-wide by construction: it is loaded per
        offering from that offering's own papers, so there is no cohort-restricted
        subset for a group token to select.
        """
        self._authorize(claims, offering_id)
        rows = get_examprep_store().search_questions(
            tenant=settings.tenant,
            offering_id=offering_id,
            query=query,
            clo_id=clo_id,
            exam_type=exam_type,
            year_from=year_from,
            min_marks=min_marks,
            limit=limit,
        )
        self._audit(claims, "search_past_questions", offering_id, len(rows))
        return rows

    def get_clos(self, offering_id: str, claims: StudentClaims) -> list[CLO]:
        """The confirmed CLO list — the spine a study plan hangs on (§7.3)."""
        self._authorize(claims, offering_id)
        rows = get_examprep_store().clos(tenant=settings.tenant, offering_id=offering_id)
        self._audit(claims, "get_clos", offering_id, len(rows))
        return rows

    def has_exam_pack(self, offering_id: str) -> bool:
        """Whether past papers were ever loaded for this offering.

        Unauthenticated on purpose, matching `has_index`: it reveals only that a
        pack exists, never its content, and the caller needs it to tell "still
        being prepared" from "nothing matched" *before* deciding what to ask for.
        """
        return get_examprep_store().has_pack(
            tenant=settings.tenant, offering_id=offering_id
        )

    def exam_pack_stats(self, offering_id: str) -> dict:
        """Counts only — how many questions, how many CLOs, the year range, and
        how many items the extractor was unsure about.

        Unauthenticated for the same reason as `has_pack`: it carries no question
        text. The caller is already JWT-verified, and this is what lets the tab
        say *why* it is empty instead of rendering a control that does nothing
        (§5.1)."""
        return get_examprep_store().stats(
            tenant=settings.tenant, offering_id=offering_id
        )


boundary = CourseIntelligenceImpl()
