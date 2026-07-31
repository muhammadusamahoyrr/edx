"""Top-up activation — the sweep's repair path (§5.4).

These tests exist because of a bug that would have shipped silently.
`write_chunks` writes every row `active=0`, and retrieval reads `active=1` only.
So a sweep that re-ingested missing blocks without activating them would log
"repaired 12 blocks", return HTTP 200, and serve none of them — the same shape as
the 226-blocks-indexed / 26-blocks-served bug from the run-scoping work. A green
ingest response is not evidence that anything became answerable.
"""

from __future__ import annotations

import pytest
from coursemate_service.knowledge.store import ChunkStore


@pytest.fixture()
def store(tmp_path):
    return ChunkStore(str(tmp_path / "t.db"))


def _rows(offering, version, usage_key, n=2):
    return [
        {
            "tenant": "default", "course_id": offering, "offering_id": offering,
            "usage_key": usage_key, "block_id": usage_key.split("@")[-1],
            "block_type": "html", "content_type": "lesson",
            "display_name": "L", "version": version, "ordinal": i,
            "text": f"photosynthesis chloroplast {i}",
        }
        for i in range(n)
    ]


def test_written_chunks_are_not_served_until_activated(store):
    """The bug itself: writing is not serving."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    assert store.indexed_usage_keys("c1") == []


def test_topup_activates_only_the_named_blocks(store):
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")

    store.write_chunks(_rows("c1", "v1", "block-v1:b"))
    store.write_chunks(_rows("c1", "v1", "block-v1:c"))
    store.activate_usage_keys("c1", "v1", ["block-v1:b"])

    served = set(store.indexed_usage_keys("c1"))
    assert served == {"block-v1:a", "block-v1:b"}, "c was not asked for and must stay dark"


def test_topup_does_not_move_the_active_pointer(store):
    """The race the sweep must not lose.

    If a full reindex swaps to v2 while the sweep is mid-flight, the sweep's
    repair must not drag the course back to v1. `swap()` would; this must not.
    """
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")
    store.write_chunks(_rows("c1", "v2", "block-v1:a"))
    store.write_chunks(_rows("c1", "v2", "block-v1:b"))
    store.swap("c1", "v2")

    # Sweep, holding the stale v1 it read before the reindex landed.
    store.write_chunks(_rows("c1", "v1", "block-v1:z"))
    store.activate_usage_keys("c1", "v1", ["block-v1:z"])

    assert store.stats("c1")["active_version"] == "v2"
    assert set(store.indexed_usage_keys("c1")) >= {"block-v1:a", "block-v1:b"}


def test_a_later_reindex_retires_topped_up_rows(store):
    """Top-up rows are written at the live version so version-scoped
    deactivation retires them for free. If they were written under their own
    version, a later reindex would leave them active forever — content that
    outlives every reindex is exactly what the sweep exists to remove."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")
    store.write_chunks(_rows("c1", "v1", "block-v1:b"))
    store.activate_usage_keys("c1", "v1", ["block-v1:b"])

    store.write_chunks(_rows("c1", "v2", "block-v1:a"))
    store.swap("c1", "v2")

    assert set(store.indexed_usage_keys("c1")) == {"block-v1:a"}


def test_chunk_count_reflects_the_topup(store):
    store.write_chunks(_rows("c1", "v1", "block-v1:a", n=2))
    store.swap("c1", "v1")
    assert store.stats("c1")["chunk_count"] == 2

    store.write_chunks(_rows("c1", "v1", "block-v1:b", n=3))
    store.activate_usage_keys("c1", "v1", ["block-v1:b"])
    assert store.stats("c1")["chunk_count"] == 5


def test_manifest_lists_served_blocks_only(store):
    """The manifest is the sweep's view of reality. If it reported inactive rows,
    the sweep would see orphans as still-served and never remove them."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.write_chunks(_rows("c1", "v1", "block-v1:b"))
    store.swap("c1", "v1")
    store.write_chunks(_rows("c1", "v2", "block-v1:c"))  # a run still in flight

    assert set(store.indexed_usage_keys("c1")) == {"block-v1:a", "block-v1:b"}


def test_prune_removes_rows_whatever_their_state(store):
    """Prune must also clear INACTIVE leftovers, or a sweep that died between
    writing and activating would write a second copy on its next run and the
    block would be cited twice."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")
    store.write_chunks(_rows("c1", "v1", "block-v1:b"))  # inactive leftover

    store.delete_usage_keys("c1", ["block-v1:b"])
    store.write_chunks(_rows("c1", "v1", "block-v1:b"))
    store.activate_usage_keys("c1", "v1", ["block-v1:b"])

    with store._lock:  # noqa: SLF001
        n = store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM chunks WHERE offering_id='c1' AND usage_key='block-v1:b'"
        ).fetchone()[0]
    assert n == 2, "one copy, not two"


def test_unpublished_block_stops_being_retrievable(store):
    """End to end, at the store level: the thing a reviewer will actually try."""
    store.write_chunks(_rows("c1", "v1", "block-v1:secret"))
    store.swap("c1", "v1")
    assert store.search("photosynthesis chloroplast", tenant="default", offering_id="c1")

    store.delete_usage_keys("c1", ["block-v1:secret"])
    assert store.search("photosynthesis chloroplast", tenant="default", offering_id="c1") == []


def test_indexed_offerings_lists_only_served_courses(store):
    """The nightly sweep's course list. It comes from the service because the
    platform-side table it originally used is written only by the bootstrap
    task — `coursemate_reindex` indexes a course and records nothing, so that
    list was empty on a stack serving 231 chunks and the sweep swept nothing."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")
    store.write_chunks(_rows("c2", "v1", "block-v1:b"))  # written, never swapped

    assert store.indexed_offerings() == ["c1"]


def test_an_emptied_offering_drops_off_the_sweep_list(store):
    """A course whose blocks were all pruned has nothing left to reconcile."""
    store.write_chunks(_rows("c1", "v1", "block-v1:a"))
    store.swap("c1", "v1")
    store.delete_usage_keys("c1", ["block-v1:a"])
    assert store.indexed_offerings() == []
