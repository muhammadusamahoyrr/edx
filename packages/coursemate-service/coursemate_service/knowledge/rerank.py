"""Reranking — design §8.2.

    Question → retrieve (lexical ∪ semantic) → merge ~20 → rerank → top 3–5 → LLM

The benchmark said why this is needed: **recall@5 = 1.000 but precision@3 =
0.306.** The right chunk is essentially always found; two of the three handed to
the model are noise. Every noisy chunk spends context budget and gives the model
more surface to go wrong on.

**Why a lexical reranker first, and not the cross-encoder §8.2 names.** The
design's target is a bge-reranker-class cross-encoder on CPU —
`BAAI/bge-reranker-base` specifically, the name `config.reranker_model` used
to carry before it was deleted for being read by nothing. That remains the
target. It is not the right *first* move here:

* It is a ~1GB download on a link measured at ~0.6 MB/s in Phase 1, and adds
  `sentence-transformers` plus torch to a 529MB image.
* It costs 100–300ms per query on CPU, on top of a generation step that already
  dominates.
* **It is unmeasurable against nothing.** Adding the expensive reranker before
  establishing whether reranking helps at all would leave us unable to say what
  the model bought.

So the *architecture* lands now — retrieve many, rerank, take top-k, behind a
protocol — with a free deterministic implementation. Swapping in a cross-encoder
becomes one class, and the benchmark can then attribute the difference to the
model rather than to the pipeline change.

**Why these signals.** BM25 already ranks by term rarity and frequency. Reranking
on the same signal would reorder nothing, so the reranker scores what BM25 does
not see:

* **coverage** — what fraction of the question is present. BM25 rewards one rare
  term appearing often; coverage rewards answering the whole question.
* **proximity** — whether the question's terms appear *near each other*. A chunk
  mentioning "cohorts" in one paragraph and "content groups" three paragraphs
  later is weaker evidence than one sentence containing both.
* **title match** — Open edX blocks carry a `display_name` an instructor chose.
  A lesson literally called "Transcripts" is strong evidence for a question about
  transcripts, and it is metadata BM25 never sees because we index body text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .store import StoredChunk, _content_terms

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RerankWeights:
    """Tuned against the gold set, not guessed.

    Coverage dominates because it is what the confidence gate reads: a reranker
    that promoted a chunk the gate then rejected would be actively harmful.
    """

    coverage: float = 0.60
    proximity: float = 0.15
    title: float = 0.25


class Reranker(Protocol):
    def rerank(self, query: str, chunks: list[StoredChunk], top_k: int) -> list[StoredChunk]:
        ...


def _proximity(query_terms: set[str], text: str, window: int = 40) -> float:
    """Best fraction of query terms co-occurring inside a sliding window.

    Cheap stand-in for what a cross-encoder learns properly: that terms scattered
    across a long passage are weaker evidence than terms in one sentence.
    """
    if not query_terms:
        return 0.0
    words = [w for w in _WORD.findall(text.lower())]
    if not words:
        return 0.0
    stems = [next(iter(_content_terms(w)), w) for w in words]

    best = 0.0
    for start in range(0, max(1, len(stems) - window + 1), max(1, window // 2)):
        chunk = set(stems[start : start + window])
        hit = len(query_terms & chunk) / len(query_terms)
        best = max(best, hit)
    return best


def _title_match(query_terms: set[str], display_name: str | None) -> float:
    if not display_name or not query_terms:
        return 0.0
    title_terms = _content_terms(display_name)
    if not title_terms:
        return 0.0
    # Fraction of the TITLE matched, not of the query: a short precise title
    # ("Transcripts") fully matched is strong evidence, and dividing by the
    # query's length would dilute exactly that signal.
    return len(query_terms & title_terms) / len(title_terms)


class LexicalReranker:
    """Deterministic reranker over BM25 candidates.

    Deterministic matters as much as accurate: the benchmark must be able to
    attribute a change to the retriever rather than to sampling noise.
    """

    def __init__(self, weights: RerankWeights | None = None) -> None:
        self.w = weights or RerankWeights()

    def rerank(self, query: str, chunks: list[StoredChunk], top_k: int) -> list[StoredChunk]:
        if not chunks:
            return []
        query_terms = _content_terms(query)
        if not query_terms:
            return chunks[:top_k]

        scored: list[tuple[float, StoredChunk]] = []
        for c in chunks:
            coverage = c.score  # already absolute query-term coverage
            proximity = _proximity(query_terms, c.text)
            title = _title_match(query_terms, c.display_name)
            combined = (
                self.w.coverage * coverage
                + self.w.proximity * proximity
                + self.w.title * title
            )
            # The chunk carries the COMBINED score forward, because the
            # confidence gate reads it. A reranker that reordered without
            # updating the score would gate on a number describing a different
            # ranking than the one the model receives.
            scored.append((combined, c.__class__(
                usage_key=c.usage_key, block_id=c.block_id,
                display_name=c.display_name, content_type=c.content_type,
                text=c.text, ordinal=c.ordinal, score=round(min(1.0, combined), 4),
            )))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [c for _, c in scored[:top_k]]


class NullReranker:
    """Pass-through. The control arm: lets the benchmark measure reranking's
    contribution by configuration rather than by editing code."""

    def rerank(self, query: str, chunks: list[StoredChunk], top_k: int) -> list[StoredChunk]:  # noqa: ARG002
        return chunks[:top_k]


def get_reranker() -> Reranker:
    from ..config import settings

    if not settings.rerank_enabled:
        # §8.2's stated degradation mode: skip reranking, take top-k by the
        # retrieval score. Measurably worse, explicitly logged, never an outage.
        log.info("reranking disabled — taking top-k by retrieval score")
        return NullReranker()
    return LexicalReranker()
