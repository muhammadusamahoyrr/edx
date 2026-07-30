"""Chunking behaviour — design §5.5.

The rule being defended: token count decides *where within a semantic unit we are
forced to split*; it never decides *what a unit is*.
"""

from __future__ import annotations

from coursemate_service.ingestion.chunking import (
    MAX_TOKENS,
    QUALITY_CLIFF_TOKENS,
    Chunk,
    chunk_block,
    estimate_tokens,
)


def test_short_block_stays_whole():
    """The common case, and the desirable one: the block boundary is already the
    semantic unit the instructor chose."""
    chunks = chunk_block("A deadlock occurs when two processes each hold a lock.")
    assert len(chunks) == 1
    assert chunks[0].split_on == "whole"


def test_empty_block_yields_nothing():
    assert chunk_block("   \n  ") == []


def test_long_block_splits_on_the_strongest_boundary_available():
    body = "\n\n".join(f"Paragraph {i}. " + ("filler words " * 60) for i in range(8))
    chunks = chunk_block(body)
    assert len(chunks) > 1
    assert all(c.split_on in {"heading", "paragraph", "list_item"} for c in chunks)


def test_headings_win_over_paragraphs():
    body = "\n".join(
        f"# Section {i}\n\n" + ("content " * 200) for i in range(4)
    )
    chunks = chunk_block(body)
    assert len(chunks) > 1
    assert chunks[0].split_on == "heading"


def test_no_chunk_approaches_the_quality_cliff():
    body = "\n\n".join("sentence " * 80 for _ in range(30))
    for chunk in chunk_block(body):
        assert chunk.est_tokens < QUALITY_CLIFF_TOKENS


def test_code_fences_are_never_split_internally():
    """A worked example separated from its problem statement is worse than an
    over-long chunk (§5.5 criterion 2)."""
    fence = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(200)) + "\n```"
    body = ("prose " * 300) + "\n\n" + fence + "\n\n" + ("more prose " * 300)
    chunks = chunk_block(body)
    fenced = [c for c in chunks if "```" in c.text]
    assert fenced, "the fence should survive somewhere"
    for chunk in fenced:
        # An opened fence is a closed fence: never cut down the middle.
        assert chunk.text.count("```") % 2 == 0


def test_ordinals_are_contiguous_from_zero():
    body = "\n\n".join(("para " * 150) for _ in range(6))
    chunks = chunk_block(body)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_no_default_overlap():
    """Overlap adds indexing cost without measurable benefit at these sizes, so
    the default is none — asserted rather than assumed, because an overlap
    silently reintroduced would show up only as a bill."""
    body = "\n\n".join(f"UNIQUE{i} " + ("x " * 150) for i in range(6))
    chunks = chunk_block(body)
    markers = [f"UNIQUE{i}" for i in range(6)]
    for marker in markers:
        assert sum(marker in c.text for c in chunks) == 1


def test_pathological_single_paragraph_is_cut_and_labelled():
    """No semantic boundary helps. Cutting on the guard rail is correct; doing it
    silently is not — the trace records `forced`."""
    body = "word " * (MAX_TOKENS * 3)
    chunks = chunk_block(body)
    assert len(chunks) > 1
    assert all(c.split_on == "forced" for c in chunks)


def test_estimate_tokens_never_returns_zero_for_nonempty():
    assert estimate_tokens("a") >= 1


def test_chunk_is_immutable():
    """Chunks are carried across pipeline stages; accidental mutation would be a
    very quiet corruption."""
    chunk = Chunk("text", 0, 1, "whole")
    try:
        chunk.text = "other"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Chunk should be frozen")
