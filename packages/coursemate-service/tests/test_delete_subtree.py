"""`delete_by_prefix` — the XBLOCK_DELETED path (§5.4).

Separate from `delete_usage_keys`, which the sweep uses with an explicit orphan
list. This one prefix-matches, because a delete event names the block that went
away and the chunks are keyed by that block.

**Why these tests exist rather than trusting the four DELETE statements.** The
method used to be raw SQL inside the route, reaching past the store into
`store._conn`, and it forgot one statement: `offering_state.chunk_count` was
never corrected. Nothing failed. `has_index()` reads that count, so an offering
whose every block had been deleted still reported an index, still appeared in
`indexed_offerings()`, and the nightly sweep kept visiting a course with nothing
in it — the shape this project keeps finding, where the failure path returns
success.

Three of the four statements are also load-bearing in ways a green `DELETE FROM
chunks` would hide: the FTS index and the access side-table are separate tables,
and a row left in either outlives the chunk it described.
"""

from __future__ import annotations

import pytest
from coursemate_service.knowledge.store import ChunkStore


@pytest.fixture()
def store(tmp_path):
    return ChunkStore(str(tmp_path / "t.db"))


def _rows(offering, version, usage_key, text, n=1, group_tokens=()):
    return [
        {
            "tenant": "default", "course_id": offering, "offering_id": offering,
            "usage_key": usage_key, "block_id": usage_key.split("@")[-1],
            "block_type": "html", "content_type": "lesson",
            "display_name": "L", "version": version, "ordinal": i,
            "text": f"{text} part {i}",
            "group_tokens": group_tokens,
        }
        for i in range(n)
    ]


def _ingest(store, offering, version, blocks):
    """Write and swap, so the offering starts in the state a live one is in."""
    for usage_key, kwargs in blocks:
        store.write_chunks(_rows(offering, version, usage_key, **kwargs))
    store.swap(offering, version)


def test_every_chunk_of_the_block_goes(store):
    """One block can be many chunks. Deleting the block must take all of them —
    a block that comes back as three chunks out of five is worse than one that
    stays, because the gap is invisible."""
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis", "n": 3})])

    assert store.delete_by_prefix("c1", "block-v1:a") == 3
    assert store.indexed_usage_keys("c1") == []


def test_other_blocks_are_untouched(store):
    _ingest(store, "c1", "v1", [
        ("block-v1:a", {"text": "photosynthesis"}),
        ("block-v1:b", {"text": "mitochondria"}),
    ])

    store.delete_by_prefix("c1", "block-v1:a")
    assert store.indexed_usage_keys("c1") == ["block-v1:b"]


def test_another_offering_is_untouched(store):
    """The prefix is scoped by offering in the same statement, not filtered
    after. Two courses can hold blocks with keys that look alike."""
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis"})])
    _ingest(store, "c2", "v1", [("block-v1:a", {"text": "photosynthesis"})])

    store.delete_by_prefix("c1", "block-v1:a")
    assert store.indexed_usage_keys("c2") == ["block-v1:a"]


def test_chunk_count_is_corrected(store):
    """The statement that was missing.

    `has_index()` and `indexed_offerings()` both read this count, so leaving it
    stale kept an emptied course on the nightly sweep's list and made the tutor
    report an index it no longer had.
    """
    _ingest(store, "c1", "v1", [
        ("block-v1:a", {"text": "photosynthesis", "n": 2}),
        ("block-v1:b", {"text": "mitochondria", "n": 3}),
    ])
    assert store.stats("c1")["chunk_count"] == 5

    store.delete_by_prefix("c1", "block-v1:a")
    assert store.stats("c1")["chunk_count"] == 3


def test_an_emptied_offering_stops_claiming_an_index(store):
    """The consequence of the above, stated the way a caller sees it."""
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis"})])
    assert store.has_index("c1")

    store.delete_by_prefix("c1", "block-v1:a")
    assert not store.has_index("c1")
    assert store.indexed_offerings() == []


def test_deleted_content_is_no_longer_retrievable(store):
    """The check a reviewer would actually run.

    `chunks_fts` is a separate table, and retrieval matches against it before
    joining `chunks`. A row left there survives its chunk, and the delete would
    look complete in every count while the text stayed searchable.
    """
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis chloroplast"})])
    assert store.search("photosynthesis chloroplast", tenant="default", offering_id="c1")

    store.delete_by_prefix("c1", "block-v1:a")
    assert store.search("photosynthesis chloroplast", tenant="default", offering_id="c1") == []


def test_access_tokens_go_with_the_chunk(store):
    """`chunk_groups` is keyed on the chunk's rowid, and SQLite reuses rowids.

    A restriction left behind therefore does not merely leak storage — it
    reattaches to whatever chunk is written next, hiding an unrestricted block
    from every student who lacks a group they were never meant to need.
    """
    _ingest(store, "c1", "v1", [
        ("block-v1:a", {"text": "cohort only", "group_tokens": ("18587404:1819362822",)}),
    ])
    with store._lock:  # noqa: SLF001
        assert store._conn.execute("SELECT COUNT(*) FROM chunk_groups").fetchone()[0] == 1  # noqa: SLF001

    store.delete_by_prefix("c1", "block-v1:a")
    with store._lock:  # noqa: SLF001
        assert store._conn.execute("SELECT COUNT(*) FROM chunk_groups").fetchone()[0] == 0  # noqa: SLF001


def test_inactive_rows_go_too(store):
    """A delete is not version-scoped, and must not be.

    Chunks written by a run still in flight are inactive and invisible to
    retrieval, but that run is about to swap. Leaving them would resurrect a
    block the instructor deleted, moments after the delete reported success.
    """
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis"})])
    store.write_chunks(_rows("c1", "v2", "block-v1:a", "photosynthesis"))  # in flight

    assert store.delete_by_prefix("c1", "block-v1:a") == 2
    store.swap("c1", "v2")
    assert store.indexed_usage_keys("c1") == []


def test_a_key_that_extends_another_is_taken_too(store):
    """Characterisation, not an endorsement.

    Prefix matching is what makes this a subtree delete, and the cost is that a
    sibling whose key merely *starts with* the deleted one goes with it. Open
    edX's fixed-length block hashes make that near-impossible in practice, which
    is why the sweep's own prune path (`delete_usage_keys`) is exact-match
    instead. Pinned here so the behaviour is a decision on record rather than a
    surprise found in a course with legacy string block ids.
    """
    _ingest(store, "c1", "v1", [
        ("block-v1:intro", {"text": "photosynthesis"}),
        ("block-v1:intro_video", {"text": "mitochondria"}),
    ])

    assert store.delete_by_prefix("c1", "block-v1:intro") == 2
    assert store.indexed_usage_keys("c1") == []


def test_deleting_nothing_reports_nothing(store):
    """An unknown key is not an error — XBLOCK_DELETED fires for blocks that were
    never indexed, such as a staff-only one dropped at ingest. It must report 0
    and leave the count alone rather than recomputing it from a half-done state.
    """
    _ingest(store, "c1", "v1", [("block-v1:a", {"text": "photosynthesis", "n": 2})])

    assert store.delete_by_prefix("c1", "block-v1:nosuch") == 0
    assert store.stats("c1")["chunk_count"] == 2
    assert store.indexed_usage_keys("c1") == ["block-v1:a"]
