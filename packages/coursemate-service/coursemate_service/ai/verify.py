"""Which sentences of an answer are not supported by what we retrieved.

`FrameType.UNSUPPORTED_CLAIM` has existed in the contract since v1, the browser
has rendered it since v1, and **nothing ever emitted it.** A frame type the UI
handles and the server never sends is the same defect `boundary/interface.py`
calls out about declaring four tools while implementing one: a contract that
does not match the code, which is worse than no contract, because a reader
concludes the check exists.

It also gives citations a meaning they did not have. The pipeline emits a
citation for every retrieved chunk, so a citation says "we searched this", not
"the answer used this" — three authoritative-looking links under a sentence none
of them support. Scoring the overlap lets the citations narrow to the chunks
that actually contributed.

---

**This is a floor, not entailment, and the distinction is the whole honesty of
the thing.** It asks: do this sentence's content words appear in the material we
retrieved? That catches the common failure — a sentence about something we never
retrieved at all, which is what a model does when it falls back on its own
knowledge. It does **not** catch a fluent contradiction built from the right
vocabulary: "deadlock cannot occur when locks are ordered" and "deadlock can
occur when locks are ordered" score identically here.

`BENCHMARKS.md` already states exactly this limitation about token-overlap
groundedness. This applies the same measure at inference time and inherits the
same ceiling.

The upgrade path is a natural-language-inference model — `LettuceDetect` (MIT,
ModernBERT, returns character spans with confidence) is the closest fit and would
drop in behind this same frame. It is deliberately *not* the first step: it needs
torch, which is the objection `knowledge/rerank.py` already raises against the
cross-encoder, and adding it before this floor exists would leave us unable to
say what it bought.

---

**Why the tokenizer is duplicated from `knowledge/store.py` rather than
imported.** `.importlinter` contract 3 forbids `ai` from importing `knowledge` —
the reasoning layer reaches data only through the boundary. Twenty lines of
duplication is the correct price for that; the contract is worth more than the
DRY, and a shared helper would have to live somewhere both may import, which is
a bigger change than the thing it saves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Sentence split on terminal punctuation followed by whitespace. Deliberately
#: crude: a real segmenter buys precision we do not use, and the failure mode of
#: a bad split is one flagged sentence too many, which the student sees as a
#: cautious tutor rather than a wrong one.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

_WORD = re.compile(r"[a-z0-9]+")

#: Kept small on purpose. An aggressive stoplist strips the very words a weak
#: match fails to share, which inflates support and hides the thing we are
#: looking for. Same list as the retrieval side, same reasoning.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "these", "those", "you", "your", "can", "will", "may", "at", "by",
    "from", "not", "no", "do", "does", "did", "have", "has", "had", "which",
    "when", "what", "how", "why", "who", "where", "i", "my", "me", "we", "us",
    "about", "into", "used", "use", "there", "their", "them", "they", "also",
    "its", "so", "such", "than", "up", "out", "each", "other", "some", "any",
})


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


#: Below this many content words a sentence carries no checkable claim — "Yes.",
#: "In short:", "Hope that helps." Flagging them would train students to ignore
#: the marker, which costs more than the few real claims it might catch.
MIN_TERMS = 4


@dataclass(frozen=True)
class Unsupported:
    sentence: str
    #: Fraction of the sentence's content words found in the retrieved material.
    coverage: float


def unsupported_sentences(
    answer: str, chunk_texts: list[str], threshold: float
) -> list[Unsupported]:
    """Sentences whose content words are largely absent from what we retrieved.

    Compared against the UNION of retrieved chunks, not each one separately: an
    answer that correctly synthesises two lessons is grounded, and scoring per
    chunk would flag exactly the good synthesis we want the tutor to do.
    """
    if not answer.strip() or not chunk_texts:
        return []

    grounded = set()
    for text in chunk_texts:
        grounded |= content_terms(text)
    if not grounded:
        return []

    out: list[Unsupported] = []
    for raw in _SENTENCE.split(answer):
        sentence = raw.strip()
        if not sentence:
            continue
        # A question is not a claim. Socratic mode opens with one by design, and
        # flagging the tutor's own guiding question as unsupported would be both
        # wrong and very visible.
        if sentence.endswith("?"):
            continue
        terms = content_terms(sentence)
        if len(terms) < MIN_TERMS:
            continue
        coverage = len(terms & grounded) / len(terms)
        if coverage < threshold:
            out.append(Unsupported(sentence=sentence, coverage=coverage))
    return out


def supporting_chunks(answer: str, chunk_texts: list[str]) -> list[int]:
    """Indices of the chunks that share content words with the answer.

    Turns a citation from "we searched this" into "this contributed". Returns
    every index when nothing overlaps, because §8.5 makes citation mandatory —
    an answer that cannot cite is supposed to have abstained, and silently
    dropping to zero citations would break that promise in the one case where
    the student most needs to see what the answer was based on.
    """
    used = content_terms(answer)
    if not used:
        return list(range(len(chunk_texts)))
    hits = [i for i, t in enumerate(chunk_texts) if content_terms(t) & used]
    return hits or list(range(len(chunk_texts)))
