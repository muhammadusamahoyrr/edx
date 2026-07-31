"""Metric definitions, each with the reason it is measured.

Design §11.1 is the governing decision: **score retrieval and generation
separately.** Measuring only the final answer hides retrieval failures — the
documented case is a legal RAG scoring 0.91 faithfulness while missing a key
statute one time in six. Only context recall exposed the retriever.

Every metric here is deliberately *cheap and deterministic*. There is no
LLM-as-judge in this tier, for two reasons:

1. **A model grading a model does not answer "is this correct?"** It answers
   "does another model find it plausible?" §11.2 treats Ragas as a proxy to be
   validated against human ratings, not as ground truth.
2. **Determinism is what makes a benchmark a benchmark.** A judge that returns
   different scores on reruns cannot tell you whether your retrieval change
   helped or the judge drifted.

The cost is precision: token-overlap groundedness is a floor, not a verdict. That
limitation is reported alongside the number rather than buried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Words too common to indicate support. Kept small on purpose — an aggressive
#: stoplist inflates groundedness by discarding exactly the words a hallucinated
#: sentence shares with the source.
_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "these", "those", "you", "your", "can", "will", "may", "at", "by",
    "from", "not", "no", "do", "does", "have", "has", "which", "when", "what",
}

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n[-*•]\s*", text)
    return [p.strip() for p in parts if len(p.strip()) > 25]


# --- retrieval ------------------------------------------------------------


def recall_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    """Did the right block appear in the top k at all?

    **The most important retrieval metric for RAG**, and the reason it leads:
    a generator can ignore an irrelevant chunk, but it cannot invent a missing
    one. Precision failures produce a noisier prompt; recall failures produce a
    wrong or abstained answer.
    """
    if not expected:
        return float("nan")  # undefined for uncovered questions
    top = set(retrieved[:k])
    return 1.0 if top & set(expected) else 0.0


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    """Proportion of the top k that is relevant.

    Secondary, but not ignorable: every irrelevant chunk consumes context budget
    and gives the model more surface to go wrong on.
    """
    if not expected:
        return float("nan")
    top = retrieved[:k]
    if not top:
        return 0.0
    return sum(1 for r in top if r in set(expected)) / len(top)


def reciprocal_rank(retrieved: list[str], expected: list[str]) -> float:
    """1/rank of the first correct hit.

    Position matters more than a flat recall suggests: the top chunks dominate
    the prompt, and a correct chunk at rank 5 may be effectively ignored.
    """
    if not expected:
        return float("nan")
    want = set(expected)
    for i, r in enumerate(retrieved, start=1):
        if r in want:
            return 1.0 / i
    return 0.0


# --- generation -----------------------------------------------------------


@dataclass
class Groundedness:
    score: float
    supported: int
    total: int
    unsupported_sentences: list[str] = field(default_factory=list)


def groundedness(answer: str, context_chunks: list[str], threshold: float = 0.45) -> Groundedness:
    """Fraction of answer sentences whose content words appear in the context.

    This is the cheap tier §8.5 specifies: *"start with string/semantic matching
    of each assertion against the retrieved chunks — near-zero added latency, and
    it catches a meaningful share of unsupported claims."*

    Reported as a **floor**: a sentence can be paraphrased beyond the threshold
    and still be supported, so a low score is a signal to inspect rather than
    proof of fabrication. It cannot be inflated the other way, which is the
    direction that matters — an unsupported sentence rarely shares its content
    words with the source by accident.
    """
    sentences = _sentences(answer)
    if not sentences:
        return Groundedness(score=float("nan"), supported=0, total=0)

    context_terms = set()
    for chunk in context_chunks:
        context_terms |= _terms(chunk)

    supported = 0
    unsupported: list[str] = []
    for sentence in sentences:
        terms = _terms(sentence)
        if not terms:
            continue
        overlap = len(terms & context_terms) / len(terms)
        if overlap >= threshold:
            supported += 1
        else:
            unsupported.append(sentence[:120])

    total = supported + len(unsupported)
    return Groundedness(
        score=supported / total if total else float("nan"),
        supported=supported,
        total=total,
        unsupported_sentences=unsupported,
    )


def citation_correctness(cited: list[str], retrieved: list[str]) -> float:
    """Every citation must name a block that was actually retrieved.

    This catches a specific and serious failure: a citation that points at a
    block which did not inform the answer. §11.2 calls it **citation validity** —
    *does the link point at the lesson that really contains this?* — and singles
    it out because a wrong citation is worse than none: it manufactures the
    appearance of grounding.
    """
    if not cited:
        return float("nan")
    retrieved_set = set(retrieved)
    return sum(1 for c in cited if c in retrieved_set) / len(cited)


# --- abstention (both directions) -----------------------------------------


@dataclass
class AbstentionOutcome:
    false_answers: int = 0        # answered when it should have abstained
    false_abstentions: int = 0    # abstained when it should have answered
    correct_answers: int = 0
    correct_abstentions: int = 0

    @property
    def total(self) -> int:
        return (self.false_answers + self.false_abstentions
                + self.correct_answers + self.correct_abstentions)

    @property
    def false_answer_rate(self) -> float:
        """The dangerous direction. A confidently wrong answer costs a student
        more than an unnecessary 'not covered' (§8.5), so this is reported first
        and weighted heaviest."""
        denom = self.false_answers + self.correct_abstentions
        return self.false_answers / denom if denom else float("nan")

    @property
    def false_abstention_rate(self) -> float:
        """The annoying direction. Measured too, because tuning only against the
        dangerous one produces a tutor that refuses everything and scores
        perfectly."""
        denom = self.false_abstentions + self.correct_answers
        return self.false_abstentions / denom if denom else float("nan")


def percentile(values: list[float], p: float) -> float:
    """p95 rather than a mean, because latency distributions are long-tailed and
    a mean hides exactly the requests students complain about."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(int(round((p / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]
