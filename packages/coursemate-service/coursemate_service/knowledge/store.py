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

-- One row per (chunk, permitted group). A side table rather than a JSON column
-- on `chunks`, because the restriction has to be resolvable INSIDE the ranking
-- query: §6.3 requires unauthorized content never to be a *candidate*, and a
-- JSON column would force filtering after ranking, which leaks through result
-- counts and timing even when the text never reaches the student.
--
-- No rows for a chunk means unrestricted, which is the common case and costs
-- one NOT EXISTS.
CREATE TABLE IF NOT EXISTS chunk_groups (
    chunk_id    INTEGER NOT NULL,
    group_token TEXT    NOT NULL,
    PRIMARY KEY (chunk_id, group_token)
);
CREATE INDEX IF NOT EXISTS ix_chunk_groups_token ON chunk_groups(group_token);

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

#: Words too common to signal that a chunk is about the question. Kept small: an
#: aggressive stoplist inflates coverage by discarding the very words a weak
#: match fails to share.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "these", "those", "you", "your", "can", "will", "may", "at", "by",
    "from", "not", "no", "do", "does", "did", "have", "has", "had", "which",
    "when", "what", "how", "why", "who", "where", "i", "my", "me", "we", "us",
    "about", "into", "used", "use", "there", "their", "them", "they",
})

_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """Crude suffix trimming, to match FTS5's `porter` tokenizer approximately.

    Exact word comparison would understate coverage badly: FTS5 matches
    "transcripts" against a chunk containing "transcript", and a gate that
    disagreed with the retriever about what matched would abstain on questions
    the retriever answered correctly.
    """
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _content_terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


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
            # Inserted one at a time rather than with executemany, because each
            # row's access tokens need that row's id — and a chunk written
            # without its restriction would be served to everyone. Getting this
            # wrong is silent: the chunk looks fine, it is just visible to more
            # people than it should be.
            written = 0
            for row in rows:
                cur = self._conn.execute(
                    """INSERT INTO chunks
                       (tenant, course_id, offering_id, usage_key, block_id, block_type,
                        content_type, display_name, version, ordinal, text, active)
                       VALUES (:tenant,:course_id,:offering_id,:usage_key,:block_id,:block_type,
                               :content_type,:display_name,:version,:ordinal,:text,0)""",
                    row,
                )
                written += cur.rowcount
                tokens = row.get("group_tokens") or ()
                if tokens:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO chunk_groups(chunk_id, group_token) "
                        "VALUES (?, ?)",
                        [(cur.lastrowid, t) for t in tokens],
                    )
            # Keep the FTS index in step with the rows just inserted.
            self._conn.execute(
                "INSERT INTO chunks_fts(rowid, text) "
                "SELECT id, text FROM chunks WHERE version = ? AND active = 0",
                (rows[0]["version"],),
            )
            self._conn.commit()
            return written

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
                self._conn.execute(f"DELETE FROM chunk_groups WHERE chunk_id IN ({marks})", stale)
                self._conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", stale)
                self._conn.commit()
        log.info("swapped %s to version %s (%d chunks)", offering_id, version, count)

    # --- retrieval -------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tenant: str,
        offering_id: str,
        group_tokens: frozenset[str] = frozenset(),
        limit: int = 5,
    ) -> list[StoredChunk]:
        """Rank by BM25 **within a pre-filtered candidate set**.

        The tenant/offering/active filter is part of the SQL, not applied after
        ranking — §6.3 requires unauthorized content never to be a *candidate*,
        rather than merely never returned. Post-filtering would leak through
        result counts and timing even when the text never reaches the student.

        `group_tokens` extends that same rule one level down, to blocks the
        instructor restricted to a cohort or an enrollment track. An empty set is
        the honest default: a caller whose groups could not be resolved sees only
        unrestricted content, never everything.
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

        # Empty IN () is a syntax error in SQLite, so an empty group set still
        # needs a placeholder. NULL never matches a token, which gives exactly
        # the intended behaviour: the caller sees unrestricted chunks only.
        marks = ",".join("?" * len(group_tokens)) if group_tokens else "NULL"

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.usage_key, c.block_id, c.display_name, c.content_type,
                       c.text, c.ordinal, bm25(chunks_fts) AS raw
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                  AND c.tenant = ? AND c.offering_id = ? AND c.active = 1
                  AND (NOT EXISTS (SELECT 1 FROM chunk_groups g
                                   WHERE g.chunk_id = c.id)
                       OR EXISTS (SELECT 1 FROM chunk_groups g
                                  WHERE g.chunk_id = c.id
                                    AND g.group_token IN ({marks})))
                ORDER BY raw
                LIMIT ?
                """,
                (match, tenant, offering_id, *sorted(group_tokens), limit),
            ).fetchall()

        if not rows:
            return []

        # --- scoring: BM25 ORDERS, query-term coverage GATES -----------------
        #
        # An earlier version normalised BM25 against the best row of the same
        # result set:
        #
        #     best = min(r["raw"] for r in rows); score = raw / best
        #
        # which made the top hit exactly 1.0 for EVERY query, however weak. The
        # confidence gate could then never fire while any row came back, so the
        # tutor answered "explain quantum chromodynamics" from an unrelated
        # lesson. The evaluation harness caught it: false_answer_rate 1.0 with
        # groundedness 1.0 — the model faithfully grounded its answer in an
        # irrelevant chunk, which is exactly the failure §11.1 predicts when only
        # the final answer is measured.
        #
        # The defect was using a RELATIVE quantity as an ABSOLUTE threshold.
        # BM25's magnitude is corpus- and query-dependent and is not comparable
        # across questions; it is excellent at ordering and useless as a
        # confidence value. Coverage — what fraction of the question's content
        # words the chunk actually contains — is bounded 0..1, comparable across
        # queries, and directly interpretable: 0.5 means "half the question's
        # substantive words appear here".
        query_terms = _content_terms(query)
        results: list[StoredChunk] = []
        for r in rows:
            if query_terms:
                overlap = len(query_terms & _content_terms(r["text"]))
                coverage = overlap / len(query_terms)
            else:
                coverage = 0.0
            results.append(
                StoredChunk(
                    usage_key=r["usage_key"],
                    block_id=r["block_id"],
                    display_name=r["display_name"],
                    content_type=r["content_type"],
                    text=r["text"],
                    ordinal=r["ordinal"],
                    score=round(coverage, 4),
                )
            )
        # Rows arrive in BM25 order and stay in it: BM25 ranks better than raw
        # coverage does (it weights rare terms), so ordering and gating use the
        # signal each is good at.
        return results

    def indexed_usage_keys(self, offering_id: str) -> list[str]:
        """Every distinct block currently *served* for this offering.

        The reconciliation sweep compares this against the published tree. It
        deliberately reads `active = 1` only: inactive rows belong to a
        superseded version and are already unreachable by retrieval, so counting
        them would report orphans that cannot be cited.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT usage_key FROM chunks WHERE offering_id=? AND active=1",
                (offering_id,),
            ).fetchall()
        return [r["usage_key"] for r in rows]

    def activate_usage_keys(self, offering_id: str, version: str,
                            usage_keys: list[str]) -> int:
        """Make specific blocks live WITHOUT moving the active pointer (§5.4).

        Not `swap()`. Swap is version-scoped: it deactivates every row whose
        version differs, so calling it from the sweep would revert the whole
        course to whatever version the sweep had read a moment earlier — losing
        a full reindex that landed in between. This touches only the named rows,
        so it cannot race a concurrent reindex into serving stale content.

        Rows are written at the CURRENT active version deliberately: a later
        reindex must be able to retire them along with everything else it
        replaces, which version-scoped deactivation does for free.
        """
        if not usage_keys:
            return 0
        with self._lock:
            marks = ",".join("?" * len(usage_keys))
            cur = self._conn.execute(
                f"UPDATE chunks SET active=1 WHERE offering_id=? AND version=? "
                f"AND usage_key IN ({marks})",
                (offering_id, version, *usage_keys),
            )
            self._conn.execute(
                "UPDATE offering_state SET chunk_count = "
                "(SELECT COUNT(*) FROM chunks WHERE offering_id=? AND active=1), "
                "updated_at = datetime('now') WHERE offering_id=?",
                (offering_id, offering_id),
            )
            self._conn.commit()
        log.info("sweep activated %d chunks across %d blocks in %s",
                 cur.rowcount, len(usage_keys), offering_id)
        return cur.rowcount

    def delete_usage_keys(self, offering_id: str, usage_keys: list[str]) -> int:
        """Remove specific blocks — the sweep's answer to unpublished content.

        Exact-match rather than prefix-match, unlike the delete-subtree path: the
        sweep computes an explicit orphan list, and a prefix here could take
        siblings whose keys happen to share a stem.
        """
        if not usage_keys:
            return 0
        with self._lock:
            marks = ",".join("?" * len(usage_keys))
            ids = [
                r["id"] for r in self._conn.execute(
                    f"SELECT id FROM chunks WHERE offering_id=? AND usage_key IN ({marks})",
                    (offering_id, *usage_keys),
                )
            ]
            if not ids:
                return 0
            id_marks = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({id_marks})", ids)
            self._conn.execute(f"DELETE FROM chunk_groups WHERE chunk_id IN ({id_marks})", ids)
            self._conn.execute(f"DELETE FROM chunks WHERE id IN ({id_marks})", ids)
            self._conn.execute(
                "UPDATE offering_state SET chunk_count = "
                "(SELECT COUNT(*) FROM chunks WHERE offering_id=? AND active=1), "
                "updated_at = datetime('now') WHERE offering_id=?",
                (offering_id, offering_id),
            )
            self._conn.commit()
        log.info("sweep removed %d chunks across %d blocks from %s",
                 len(ids), len(usage_keys), offering_id)
        return len(ids)

    def stats(self, offering_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT active_version, chunk_count, updated_at FROM offering_state WHERE offering_id=?",
                (offering_id,),
            ).fetchone()
        return dict(row) if row else {"active_version": None, "chunk_count": 0, "updated_at": None}

    def indexed_offerings(self) -> list[str]:
        """Every offering actually being served.

        The nightly sweep's course list (§5.4). It comes from here rather than
        from a platform-side table because the service is the only component
        that knows what it serves: `coursemate_reindex` writes chunks without
        recording any platform state, so a table-driven list swept nothing at
        all — the sweep ran nightly across zero courses and reported success.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT offering_id FROM offering_state WHERE chunk_count > 0"
            ).fetchall()
        return [r["offering_id"] for r in rows]

    def has_index(self, offering_id: str) -> bool:
        return self.stats(offering_id)["chunk_count"] > 0
