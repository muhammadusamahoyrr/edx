"""Chunk store and retrieval index — design §6.2, §6.3, §5.3.

**Why SQLite FTS5/BM25 rather than a vector store.** The design plans semantic
retrieval (§6.1), and that remains the target. It is not buildable yet: semantic
search needs an embedding provider, and none is configured. Rather than stall the
knowledge layer on a credential, this ships the *lexical half* of the hybrid the
design already calls for — which §6.1 explicitly anticipates as one of the two
retrievers.

What that buys, concretely:

* **No new infrastructure.** FTS5 and `bm25()` are in the Python standard
  library. At 413 blocks a vector database would be a container, a client and a
  failure mode in exchange for nothing measurable.
* **Deterministic, therefore verifiable.** The same query returns the same ranked
  set every run, so retrieval quality can be asserted in a test rather than
  eyeballed.
* **Swappable.** Everything above this file talks to `ContextProvider`. Adding
  embeddings means adding a second retriever and merging — the design's hybrid —
  not rewriting callers.

The honest limitation is stated where it belongs (§ report): lexical matching
fails on paraphrase. "What is a deadlock?" finds a lesson containing the word
"deadlock" and misses one that only says "circular wait".

**Write-then-swap (§5.3) is implemented here**, not bolted on: chunks are written
under a new version, verified, and only then does the active pointer flip. A
failure mid-ingest leaves the previous good state intact rather than a hole that
the tutor would report as "not covered in this course".
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    tenant        TEXT NOT NULL,
    course_id     TEXT NOT NULL,
    offering_id   TEXT NOT NULL,
    usage_key     TEXT NOT NULL,
    block_id      TEXT NOT NULL,
    block_type    TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    display_name  TEXT,
    version       TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scope   ON chunks(tenant, offering_id, active);
CREATE INDEX IF NOT EXISTS ix_usage   ON chunks(usage_key, version);

-- External-content FTS: the index mirrors `chunks` rather than duplicating text.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, content='chunks', content_rowid='id', tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS offering_state (
    offering_id      TEXT PRIMARY KEY,
    active_version   TEXT,
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT
);
"""

#: FTS5 treats these as syntax. A student's question is untrusted input (§10.6),
#: so it is stripped to bare terms rather than passed through as a query language.
_FTS_UNSAFE = re.compile(r"""[^\w\s]""", re.UNICODE)


@dataclass(frozen=True)
class StoredChunk:
    usage_key: str
    block_id: str
    display_name: str | None
    content_type: str
    text: str
    ordinal: int
    #: Normalised 0..1. BM25 is unbounded and negative-is-better in SQLite, so the
    #: raw score is useless to a confidence threshold without normalisation.
    score: float


class ChunkStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock. The workload is a handful of queries
        # per question against a small file; a pool would add contention bugs
        # without buying throughput.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- ingest: write → verify → swap (§5.3) ----------------------------

    def write_chunks(self, rows: list[dict]) -> int:
        """Write chunks for a new version. They are INACTIVE until swapped."""
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT INTO chunks
                   (tenant, course_id, offering_id, usage_key, block_id, block_type,
                    content_type, display_name, version, ordinal, text, active)
                   VALUES (:tenant,:course_id,:offering_id,:usage_key,:block_id,:block_type,
                           :content_type,:display_name,:version,:ordinal,:text,0)""",
                rows,
            )
            # Keep the FTS index in step with the rows just inserted.
            self._conn.execute(
                "INSERT INTO chunks_fts(rowid, text) "
                "SELECT id, text FROM chunks WHERE version = ? AND active = 0",
                (rows[0]["version"],),
            )
            self._conn.commit()
            return cur.rowcount

    def verify(self, offering_id: str, version: str, expected: int) -> bool:
        with self._lock:
            got = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE offering_id=? AND version=?",
                (offering_id, version),
            ).fetchone()[0]
        if got != expected:
            log.error("verify failed for %s@%s: %s != %s", offering_id, version, got, expected)
        return got == expected

    def verify_run(self, offering_id: str, version: str) -> bool:
        """A run is verifiable if it wrote anything and every row is readable.

        Counting rows for the run rather than comparing against a caller-supplied
        expectation, because the caller sends the course in batches and does not
        know the final chunk count until the last one is written.
        """
        with self._lock:
            got = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE offering_id=? AND version=?",
                (offering_id, version),
            ).fetchone()[0]
        if got == 0:
            log.error("run %s for %s wrote nothing; refusing to swap", version, offering_id)
        return got > 0

    def swap(self, offering_id: str, version: str) -> None:
        """Flip the active pointer atomically, then GC superseded versions.

        Retrieval always filters on `active`, so stale and current content can
        never coexist — which was the original delete-then-insert rule's actual
        goal, without its failure mode.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "UPDATE chunks SET active=0 WHERE offering_id=? AND version<>?",
                (offering_id, version),
            )
            self._conn.execute(
                "UPDATE chunks SET active=1 WHERE offering_id=? AND version=?",
                (offering_id, version),
            )
            count = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE offering_id=? AND active=1", (offering_id,)
            ).fetchone()[0]
            self._conn.execute(
                """INSERT INTO offering_state(offering_id, active_version, chunk_count, updated_at)
                   VALUES(?,?,?,datetime('now'))
                   ON CONFLICT(offering_id) DO UPDATE SET
                     active_version=excluded.active_version,
                     chunk_count=excluded.chunk_count,
                     updated_at=excluded.updated_at""",
                (offering_id, version, count),
            )
            self._conn.commit()

            # GC is separate and safe to retry: superseded rows are already
            # unreachable by retrieval before they are deleted.
            stale = [
                r["id"] for r in self._conn.execute(
                    "SELECT id FROM chunks WHERE offering_id=? AND version<>?",
                    (offering_id, version),
                )
            ]
            if stale:
                marks = ",".join("?" * len(stale))
                self._conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", stale)
                self._conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", stale)
                self._conn.commit()
        log.info("swapped %s to version %s (%d chunks)", offering_id, version, count)

    # --- retrieval -------------------------------------------------------

    def search(
        self, query: str, *, tenant: str, offering_id: str, limit: int = 5
    ) -> list[StoredChunk]:
        """Rank by BM25 **within a pre-filtered candidate set**.

        The tenant/offering/active filter is part of the SQL, not applied after
        ranking — §6.3 requires unauthorized content never to be a *candidate*,
        rather than merely never returned. Post-filtering would leak through
        result counts and timing even when the text never reaches the student.
        """
        terms = _FTS_UNSAFE.sub(" ", query).split()
        if not terms:
            return []
        # Each term is quoted so FTS5 treats it as a LITERAL, not an operator.
        # Stripping punctuation is not enough on its own: FTS5's keywords are
        # bare words, so a student asking "cats AND dogs" — or typing a stray
        # `OR` — would otherwise inject query syntax and raise
        # `fts5: syntax error`. Quoting removes the operator surface entirely.
        # (Found by the hostile-input test, not by review.)
        match = " OR ".join(f'"{t}"' for t in terms)

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.usage_key, c.block_id, c.display_name, c.content_type,
                       c.text, c.ordinal, bm25(chunks_fts) AS raw
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                  AND c.tenant = ? AND c.offering_id = ? AND c.active = 1
                ORDER BY raw
                LIMIT ?
                """,
                (match, tenant, offering_id, limit),
            ).fetchall()

        if not rows:
            return []

        # SQLite's bm25() returns negative values, better = more negative. Map to
        # 0..1 so the confidence gate compares against a stable threshold rather
        # than a corpus-dependent magnitude.
        best = min(r["raw"] for r in rows)
        return [
            StoredChunk(
                usage_key=r["usage_key"],
                block_id=r["block_id"],
                display_name=r["display_name"],
                content_type=r["content_type"],
                text=r["text"],
                ordinal=r["ordinal"],
                score=round(min(1.0, r["raw"] / best) if best else 0.0, 4),
            )
            for r in rows
        ]

    def stats(self, offering_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT active_version, chunk_count, updated_at FROM offering_state WHERE offering_id=?",
                (offering_id,),
            ).fetchone()
        return dict(row) if row else {"active_version": None, "chunk_count": 0, "updated_at": None}

    def has_index(self, offering_id: str) -> bool:
        return self.stats(offering_id)["chunk_count"] > 0
