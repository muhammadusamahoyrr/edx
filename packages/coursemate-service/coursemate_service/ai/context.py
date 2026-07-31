"""The retrieval seam — where RAG lands in Phase 6.

This module exists **now**, empty of retrieval, so that adding RAG later changes
no API, no route, and no frame type. The pipeline already asks for context and
already threads citations through; Phase 6 supplies a real implementation of this
one protocol and nothing above it moves.

Two decisions are baked in here on purpose:

* **Context is fetched, never invented.** The pipeline cannot generate an answer
  without asking this provider first, so there is no code path where retrieval is
  "skipped for speed". When the real provider lands, grounding is on by
  construction.

* **The provider decides confidence, not the caller.** `ContextResult` carries the
  top score, so the abstention gate (§8.5) reads a number the retriever produced
  rather than one the generator guessed.
"""

from __future__ import annotations

from typing import Protocol

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import Citation
from pydantic import BaseModel, Field


class ContextChunk(BaseModel):
    text: str
    citation: Citation
    score: float


class ContextResult(BaseModel):
    chunks: list[ContextChunk] = Field(default_factory=list)
    #: Top reranker score. The abstention gate compares this against tau (§8.5).
    top_score: float = 0.0
    #: True when the course has no index at all — a different state from "nothing
    #: matched", and the student is told something different (§5.1).
    index_missing: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.chunks


class ContextProvider(Protocol):
    """Retrieval contract. Phase 6 implements this against the boundary."""

    async def fetch(self, question: str, claims: StudentClaims) -> ContextResult:
        ...


class NullContextProvider:
    """Phase 5 placeholder: no retrieval yet.

    Returns an empty result rather than raising, so the pipeline's grounding
    behaviour is exercised end to end from day one. With `require_grounding`
    enabled this makes the tutor abstain on every question — which is the
    *correct* behaviour for a tutor that cannot yet read the course, and is why
    the flag exists rather than being hardcoded.
    """

    async def fetch(self, question: str, claims: StudentClaims) -> ContextResult:  # noqa: ARG002
        return ContextResult(chunks=[], top_score=0.0, index_missing=True)
