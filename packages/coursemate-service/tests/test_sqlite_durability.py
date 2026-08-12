"""WAL and `busy_timeout`, asserted against the running database.

The reason these are tested at all rather than taken on trust: `PRAGMA
journal_mode=WAL` **can decline, and declines by returning the mode it kept
instead of raising**. Code that sets it and moves on believes it has concurrency
it does not have, and the symptom appears much later as `database is locked`
under load — a green result hiding a broken setting, which is the exact failure
shape this project keeps finding.

So every assertion below reads the pragma back out of SQLite. None of them assert
that a line of code exists.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from coursemate_service.knowledge.examprep_store import ExamPrepStore
from coursemate_service.knowledge.sqlite_setup import (
    BUSY_TIMEOUT_MS,
    apply_durability_pragmas,
    connect,
)
from coursemate_service.knowledge.store import ChunkStore


@pytest.fixture
def opened():
    """Open connections, closed at the end of the test.

    Not tidiness: an unclosed connection is reported as a `ResourceWarning` at
    collection time, and a suite that emits a hundred of them is a suite where
    the one warning that matters is invisible.
    """
    conns = []

    def open_one(path):
        conn = connect(path)
        conns.append(conn)
        return conn

    yield open_one
    for c in conns:
        c.close()


def _mode(conn) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _timeout(conn) -> int:
    return int(conn.execute("PRAGMA busy_timeout").fetchone()[0])


# --- the helper itself -----------------------------------------------------


def test_wal_is_enabled_on_a_file_database(tmp_path, opened):
    assert _mode(opened(tmp_path / "x.db")) == "wal"


def test_the_busy_timeout_is_set(tmp_path, opened):
    assert _timeout(opened(tmp_path / "x.db")) == BUSY_TIMEOUT_MS


def test_the_helper_reports_what_sqlite_actually_did(tmp_path):
    """Read back, not assumed — the whole reason this returns anything."""
    with sqlite3.connect(str(tmp_path / "x.db")) as conn:
        mode, timeout = apply_durability_pragmas(conn)
    conn.close()
    assert (mode, timeout) == ("wal", BUSY_TIMEOUT_MS)


def test_an_in_memory_database_reports_that_it_declined(tmp_path):
    """`:memory:` cannot do WAL and says so by returning `memory`. Reported
    honestly rather than papered over: an in-memory database is per-connection
    and has nothing to race, but a test asserting WAL here would pass on a lie."""
    conn = sqlite3.connect(":memory:")
    mode, timeout = apply_durability_pragmas(conn)
    conn.close()

    assert mode == "memory"
    assert timeout == BUSY_TIMEOUT_MS, "the timeout still applies"


def test_wal_survives_reopening(tmp_path):
    """It is a property of the FILE, recorded in the header — which is why
    setting it is idempotent, and why it cannot be inferred from the code."""
    path = tmp_path / "x.db"
    connect(path).close()

    plain = sqlite3.connect(str(path))
    try:
        assert _mode(plain) == "wal"
    finally:
        plain.close()


# --- both stores, consistently ---------------------------------------------


def test_the_chunk_store_uses_wal(tmp_path):
    store = ChunkStore(tmp_path / "chunks.db")
    assert _mode(store._conn) == "wal"
    assert _timeout(store._conn) == BUSY_TIMEOUT_MS


def test_the_exam_prep_store_uses_wal(tmp_path):
    store = ExamPrepStore(tmp_path / "examprep.db")
    assert _mode(store._conn) == "wal"
    assert _timeout(store._conn) == BUSY_TIMEOUT_MS


def test_both_stores_agree(tmp_path):
    """One helper, so they cannot drift into different durability settings —
    which would be invisible until one started returning `database is locked`
    under load and the other did not."""
    chunks = ChunkStore(tmp_path / "chunks.db")
    exams = ExamPrepStore(tmp_path / "examprep.db")

    assert _mode(chunks._conn) == _mode(exams._conn) == "wal"
    assert _timeout(chunks._conn) == _timeout(exams._conn) == BUSY_TIMEOUT_MS


# --- what the settings actually buy ----------------------------------------


def test_a_reader_is_not_blocked_by_an_open_writer(tmp_path, opened):
    """The property WAL exists for, exercised rather than asserted.

    On the default rollback journal this read raises `database is locked`
    immediately. Under WAL it returns the last committed state — which is the
    difference between a reindex being invisible to students and being a 500.
    """
    path = tmp_path / "x.db"
    writer = opened(path)
    writer.execute("CREATE TABLE t (v INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()

    reader = opened(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (2)")
    try:
        # Committed state only: the uncommitted 2 must not be visible.
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        writer.rollback()


def test_a_blocked_writer_waits_instead_of_failing_instantly(tmp_path, opened):
    """What `busy_timeout` buys. WAL does not let two writers proceed at once,
    so without a timeout the second fails the moment it collides — turning a few
    milliseconds of contention into an error the student sees."""
    path = tmp_path / "x.db"
    setup = opened(path)
    setup.execute("CREATE TABLE t (v INTEGER)")
    setup.commit()

    holder = opened(path)
    holder.execute("BEGIN IMMEDIATE")

    second = opened(path)
    # Shorten it so the test does not sit here for the full 5 s.
    second.execute("PRAGMA busy_timeout = 400")

    released = threading.Event()

    def release_soon():
        time.sleep(0.15)
        holder.rollback()
        released.set()

    threading.Thread(target=release_soon, daemon=True).start()
    started = time.monotonic()
    second.execute("BEGIN IMMEDIATE")   # must wait, not raise
    waited = time.monotonic() - started
    second.rollback()

    assert released.is_set()
    assert waited >= 0.1, "it returned too fast to have waited for the lock"


def test_a_writer_still_gives_up_eventually(tmp_path, opened):
    """Bounded, deliberately. A timeout long enough to absorb a checkpoint but
    short enough that a genuine deadlock surfaces as an error rather than
    hanging a worker forever."""
    path = tmp_path / "x.db"
    setup = opened(path)
    setup.execute("CREATE TABLE t (v INTEGER)")
    setup.commit()

    holder = opened(path)
    holder.execute("BEGIN IMMEDIATE")

    second = opened(path)
    second.execute("PRAGMA busy_timeout = 100")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        second.execute("BEGIN IMMEDIATE")
    holder.rollback()
