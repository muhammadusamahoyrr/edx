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
        """Async wrapper. SQLite is blocking, so the whole retrieval runs in a
        worker thread — one slow query must not stall every other student's
        stream."""
        return await asyncio.to_thread(self.fetch_sync, question, claims)

    async def fetch_outline(self, claims: StudentClaims) -> ContextResult:
        """Async wrapper, for the same reason `fetch` has one: SQLite blocks."""
        return await asyncio.to_thread(self.fetch_outline_sync, claims)

    def fetch_outline_sync(self, claims: StudentClaims) -> ContextResult:
        """The course's author-written overview, or an empty result.

        Deliberately shaped as a `ContextResult` so the pipeline's existing
        citation construction applies unchanged — the same `Citation`, built from
        the same modulestore read, pointing at the same deep link. An outline
        answer cites exactly like any other answer because it *is* course
        content; only the way it was selected differs.

        `top_score` is left at its default and must not be gated on: these blocks
        were not ranked, so there is no confidence to compare against tau. The
        gate is skipped on this path — see `pipeline.stream` — rather than being
        fed a number that would only look meaningful.
        """
        offering_id = claims.offering_id

        if not boundary.has_index(offering_id):
            log.info("no index for offering %s", offering_id)
            return ContextResult(chunks=[], top_score=0.0, index_missing=True)

        try:
            blocks = boundary.course_summary_blocks(offering_id, claims)
        except AuthorizationError as exc:
            # Identical handling to `fetch_sync`: denied scope returns EMPTY, and
            # `index_version` stays None so nothing can be cached against it.
            log.warning("authorization denied: %s", exc)
            return ContextResult(chunks=[], top_score=0.0, index_missing=False)

        version = boundary.index_version(offering_id)

        return ContextResult(
            index_version=version,
            index_missing=False,
            chunks=[
                ContextChunk(
                    text=b.text,
                    citation=Citation(
                        usage_key=b.usage_key,
                        display_name=b.display_name or b.block_id,
                        url=_citation_url(b.usage_key, claims.course_id),
                    ),
                    score=b.score,
                )
                for b in blocks
            ],
        )

    def fetch_sync(
        self, question: str, claims: StudentClaims, limit: int | None = None
    ) -> ContextResult:
        """The retrieval itself, synchronous.

        Split out from `fetch` when the exam-prep agent needed it: the agent's
        tool handlers already run in a worker thread, so wrapping this in a
        coroutine only to await it from inside that thread would need a second
        event loop. Same code, same boundary call, same gate input — the async
        path is now a two-line wrapper rather than a parallel implementation.
        """
        limit = limit or self.limit
        offering_id = claims.offering_id

        # Distinguish "no index" from "nothing matched" BEFORE searching. They
        # produce different messages to the student and only one of them means
        # "come back later" (§5.1).
        if not boundary.has_index(offering_id):
            log.info("no index for offering %s", offering_id)
            return ContextResult(chunks=[], top_score=0.0, index_missing=True)

        try:
            chunks = boundary.retrieve_course_context(
                question, offering_id, claims, limit
            )
        except AuthorizationError as exc:
            # Denied scope returns EMPTY, never someone else's content. The
            # pipeline then abstains, which is the correct outcome: a student
            # asking outside their enrollment gets "not covered", not an error
            # that confirms the content exists.
            #
            # **`index_version` is deliberately left None here**, and that is a
            # security property, not an omission: the response cache only builds
            # a key when it has a version, so a caller who failed authorization
            # cannot read from the cache or write to it. The check does not need
            # to be repeated in the cache — a denied caller never reaches it.
            log.warning("authorization denied: %s", exc)
            return ContextResult(chunks=[], top_score=0.0, index_missing=False)

        # Read once, after the index is known to exist, and carry it on the
        # result. The response cache needs it in its key; fetching it separately
        # from the pipeline would be a second boundary call answering a question
        # this one already had to ask.
        version = boundary.index_version(offering_id)

        if not chunks:
            return ContextResult(
                chunks=[], top_score=0.0, index_missing=False, index_version=version
            )

        return ContextResult(
            index_version=version,
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
