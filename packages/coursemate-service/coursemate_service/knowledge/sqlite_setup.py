"""How this service opens a SQLite file, in one place.

Both stores are read-mostly and written by a background job while requests are
being served. On the default rollback journal that combination is a reader/writer
lock: an ingest holds the database and every concurrent read gets
`database is locked` — immediately, with no wait, as a 500 the student sees.

Two pragmas fix it, and they fix different halves:

* **WAL** lets readers and one writer proceed at the same time. Readers see the
  last committed state instead of blocking, which is exactly the shape of this
  workload — a reindex writing 227 chunks while students are asking questions.
* **`busy_timeout`** covers what WAL does not. Two *writers* still serialise, and
  a checkpoint briefly needs exclusive access. Without a timeout SQLite fails
  those instantly rather than waiting the few milliseconds they take. 5 s is far
  longer than any write here and still short enough to surface a real deadlock
  rather than hang a worker forever.

**WAL is a property of the file, not of the connection.** It is recorded in the
database header and survives reopening, so setting it here is idempotent. It also
means the setting cannot be assumed from the code alone — hence
`apply_durability_pragmas` returns what SQLite actually did, and the tests assert
against the running database rather than against this file.

**`:memory:` cannot do WAL**, and asking returns `"memory"` rather than raising.
That is reported honestly instead of being papered over: an in-memory database is
per-connection and has no second process to race, so it needs neither pragma —
but a test that asserted WAL against `:memory:` would pass on a lie, and this
project has been bitten by exactly that shape of green result before.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

#: Milliseconds a blocked statement waits before giving up. See the module
#: docstring: long enough to absorb a checkpoint, short enough that a genuine
#: deadlock still surfaces as an error instead of a hung worker.
BUSY_TIMEOUT_MS = 5000


def apply_durability_pragmas(conn: sqlite3.Connection) -> tuple[str, int]:
    """Set WAL and `busy_timeout`. Returns what SQLite reports afterwards.

    Read back rather than assumed. `PRAGMA journal_mode=WAL` can decline — on an
    in-memory database, or on a filesystem without shared-memory support — and it
    declines by *returning the mode it kept*, not by raising. A caller that
    trusted the write would believe it had concurrency it does not have.
    """
    # Outside any transaction: journal_mode=WAL is a no-op inside one, and it
    # reports the old mode when it refuses, so this would silently do nothing.
    # Interpolated, not bound: SQLite does not accept a parameter in a PRAGMA.
    # The value is a module constant, never caller input.
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    if str(mode).lower() != "wal":
        # Not an error. `:memory:` is single-connection by definition and has
        # nothing to race; say so once rather than failing a dev run.
        log.info("sqlite: journal_mode is %r, not WAL (expected for :memory:)", mode)
    return str(mode).lower(), int(timeout)


def connect(path: str | Path) -> sqlite3.Connection:
    """The one way this service opens a database.

    `check_same_thread=False` because both stores hand a single connection to a
    thread pool behind their own lock — the lock, not the connection, is what
    serialises access.
    """
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_durability_pragmas(conn)
    return conn
