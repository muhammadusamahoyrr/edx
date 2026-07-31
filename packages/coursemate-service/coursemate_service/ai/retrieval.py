"""The real ContextProvider — replaces NullContextProvider (design §6.5, §8.5).

Note what this class does **not** do: it does not touch the store. It calls the
CourseIntelligence boundary, which is what guarantees identity resolution, scope
checking, filter-before-ranking and auditing happen on every retrieval. The
reasoning layer reaching around that boundary is exactly the failure §6.5 exists
to prevent, and `.importlinter` enforces it.

Everything above this file is unchanged: the pipeline asked for context in Phase
5 and still asks for it identically. Swapping `NullContextProvider` for this class
is a one-line change in the pipeline's default — the seam held.
"""

from __future__ import annotations

import asyncio
import logging

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import Citation

from ..boundary.impl import AuthorizationError, boundary
from ..config import settings
from .context import ContextChunk, ContextResult

log = logging.getLogger(__name__)


def _citation_url(usage_key: str, course_id: str) -> str:
    """Deep link to the source block.

    Built from the same modulestore read that produced the chunk rather than from
    a later Course Blocks API call — those two were observed to disagree, and a
    citation that points at a stale tree is worse than none.
    """
    return f"/courses/{course_id}/jump_to/{usage_key}"


class CourseContextProvider:
    """Retrieval over published course content, scoped to the caller."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit or settings.rerank_top_k

    async def fetch(self, question: str, claims: StudentClaims) -> ContextResult:
        offering_id = claims.offering_id

        # Distinguish "no index" from "nothing matched" BEFORE searching. They
        # produce different messages to the student and only one of them means
        # "come back later" (§5.1).
        if not await asyncio.to_thread(boundary.has_index, offering_id):
            log.info("no index for offering %s", offering_id)
            return ContextResult(chunks=[], top_score=0.0, index_missing=True)

        try:
            # SQLite is blocking; keep the event loop free so one slow retrieval
            # cannot stall every other student's stream.
            chunks = await asyncio.to_thread(
                boundary.retrieve_course_context, question, offering_id, claims, self.limit
            )
        except AuthorizationError as exc:
            # Denied scope returns EMPTY, never someone else's content. The
            # pipeline then abstains, which is the correct outcome: a student
            # asking outside their enrollment gets "not covered", not an error
            # that confirms the content exists.
            log.warning("authorization denied: %s", exc)
            return ContextResult(chunks=[], top_score=0.0, index_missing=False)

        if not chunks:
            return ContextResult(chunks=[], top_score=0.0, index_missing=False)

        return ContextResult(
            chunks=[
                ContextChunk(
                    text=c.text,
                    citation=Citation(
                        usage_key=c.usage_key,
                        display_name=c.display_name or c.block_id,
                        url=_citation_url(c.usage_key, claims.course_id),
                    ),
                    score=c.score,
                )
                for c in chunks
            ],
            top_score=max(c.score for c in chunks),
            index_missing=False,
        )
