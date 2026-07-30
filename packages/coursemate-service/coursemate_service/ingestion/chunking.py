"""Chunking — design §5.5.

Three ordered criteria, and the order is the whole design:

1. **Open edX block boundaries are authoritative.** A leaf block is a unit the
   instructor deliberately authored as one idea, and it is our citation key and
   our swap key. Two blocks are never merged into one chunk, because that chunk
   could no longer cite a single usage_key.
2. **Semantic boundaries within a block** — headings, list groups, worked
   examples, code fences. A definition is never split from its term.
3. **Token range as a guard rail** — ~512-1024 tokens, no default overlap,
   staying under the ~2500-token quality cliff.

In one line: token count decides *where within a semantic unit we are forced to
split*; it never decides *what a unit is*.

Criterion 1 is not enforced here at all — it is enforced by the wire format
(`IngestRequest.blocks` is one record per leaf), so this module only ever sees
one block's text and is structurally incapable of merging across blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Guard rails, not targets (§5.5 criterion 3).
TARGET_TOKENS = 512
MAX_TOKENS = 1024
#: Beyond this, retrieval quality measurably degrades.
QUALITY_CLIFF_TOKENS = 2500
#: Overlap adds indexing cost without measurable benefit at these sizes.
DEFAULT_OVERLAP = 0

#: Roughly four characters per token for English prose. Deliberately crude: this
#: is a guard rail, and a real tokenizer here would buy precision we do not use.
CHARS_PER_TOKEN = 4

#: Split points, strongest semantic boundary first. Order matters — we take the
#: strongest boundary that yields a chunk under MAX_TOKENS.
_BOUNDARIES: tuple[tuple[str, str], ...] = (
    ("heading", r"\n(?=#{1,6}\s)"),
    ("paragraph", r"\n\s*\n"),
    ("list_item", r"\n(?=\s*(?:[-*+]|\d+\.)\s)"),
    ("sentence", r"(?<=[.!?])\s+(?=[A-Z])"),
)

#: Fenced code and worked examples are never split internally: a worked example
#: separated from its problem statement is worse than an over-long chunk.
_ATOMIC = re.compile(r"```.*?```", re.DOTALL)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Chunk:
    text: str
    #: Position within the block, so citations can point at a place in a lesson.
    ordinal: int
    est_tokens: int
    #: Which boundary produced this split, for the trace (§11.4). "whole" means
    #: the block was short enough to stay intact, which is the common case.
    split_on: str


def _protect_atomic(text: str) -> tuple[str, dict[str, str]]:
    """Replace atomic spans with placeholders so no boundary rule can cut them."""
    vault: dict[str, str] = {}

    def stash(match: re.Match[str]) -> str:
        key = f"\x00ATOMIC{len(vault)}\x00"
        vault[key] = match.group(0)
        return key

    return _ATOMIC.sub(stash, text), vault


def _restore(text: str, vault: dict[str, str]) -> str:
    for key, original in vault.items():
        text = text.replace(key, original)
    return text


def _split_on(text: str, pattern: str) -> list[str]:
    return [part for part in re.split(pattern, text) if part.strip()]


def _pack(segments: list[str], boundary: str, start_ordinal: int) -> list[Chunk]:
    """Greedily fill chunks to TARGET_TOKENS without exceeding MAX_TOKENS."""
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffered = 0

    for segment in segments:
        size = estimate_tokens(segment)
        if buffer and buffered + size > MAX_TOKENS:
            joined = "\n\n".join(buffer)
            chunks.append(
                Chunk(joined, start_ordinal + len(chunks), estimate_tokens(joined), boundary)
            )
            buffer, buffered = [], 0
        buffer.append(segment)
        buffered += size
        if buffered >= TARGET_TOKENS:
            joined = "\n\n".join(buffer)
            chunks.append(
                Chunk(joined, start_ordinal + len(chunks), estimate_tokens(joined), boundary)
            )
            buffer, buffered = [], 0

    if buffer:
        joined = "\n\n".join(buffer)
        chunks.append(
            Chunk(joined, start_ordinal + len(chunks), estimate_tokens(joined), boundary)
        )
    return chunks


def chunk_block(text: str) -> list[Chunk]:
    """Chunk the text of exactly one leaf block.

    Short blocks stay whole — which is the common case and the desirable one,
    since the block boundary is already the semantic unit the instructor chose.
    """
    stripped = text.strip()
    if not stripped:
        return []

    if estimate_tokens(stripped) <= MAX_TOKENS:
        return [Chunk(stripped, 0, estimate_tokens(stripped), "whole")]

    protected, vault = _protect_atomic(stripped)

    for name, pattern in _BOUNDARIES:
        segments = _split_on(protected, pattern)
        if len(segments) < 2:
            continue
        if max(estimate_tokens(s) for s in segments) > MAX_TOKENS:
            # This boundary still leaves an over-long piece; try a finer one.
            continue
        return [
            Chunk(_restore(c.text, vault), c.ordinal, estimate_tokens(_restore(c.text, vault)), name)
            for c in _pack(segments, name, 0)
        ]

    # No semantic boundary helped — a single enormous paragraph, or one atomic
    # span larger than MAX_TOKENS. Cut on the guard rail and say so in the trace
    # rather than silently emitting something past the quality cliff.
    restored = _restore(protected, vault)
    window = MAX_TOKENS * CHARS_PER_TOKEN
    pieces = [restored[i : i + window] for i in range(0, len(restored), window)]
    return [
        Chunk(piece.strip(), i, estimate_tokens(piece), "forced")
        for i, piece in enumerate(pieces)
        if piece.strip()
    ]
