"""Chunk store and retrieval index — design §6.2, §6.3, §5.3.

**Why SQLite FTS5/BM25 rather than a vector store.** The design plans semantic
retrieval (§6.1), and that remains the target. It is not buildable yet: semantic
search needs an embedding provider, and none is configured. Rather than stall the
knowledge layer on a credential, this ships the *lexical half* of the hybrid the
design already calls for — which §6.1 explicitly anticipates as one of the two
retrievers.

What that buys, concretely:

* **No new infrastructure.** FTS5 and `bm25()` are in the Python standard
  library. At 282 chunks a vector database would be a container, a client and a
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
import threading
from dataclasses import dataclass
from pathlib import Path

from . import sqlite_setup

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

#: **The block-level access rule, written once.** §6.3 requires unauthorized
#: content never to be a *candidate*, and every query that reaches `chunks` has
#: to enforce it identically. A second retrieval path that re-typed this clause —
#: even correctly, today — is the shape of defect this codebase keeps finding:
#: a control that is right in one path and subtly absent from the next.
#:
#: `{marks}` is filled by `_access_params`, never by a caller.
_ACCESS_CLAUSE = """
                  AND (NOT EXISTS (SELECT 1 FROM chunk_groups g
                                   WHERE g.chunk_id = c.id)
                       OR EXISTS (SELECT 1 FROM chunk_groups g
                                  WHERE g.chunk_id = c.id
                                    AND g.group_token IN ({marks})))
"""


def _access_params(group_tokens: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    """The access clause and its bind parameters, for one caller's groups.

    Empty IN () is a syntax error in SQLite, so an empty group set still needs a
    placeholder. NULL never matches a token, which gives exactly the intended
    behaviour: the caller sees unrestricted chunks only. Sorted so the parameter
    order is deterministic and a query plan is reusable.
    """
    marks = ",".join("?" * len(group_tokens)) if group_tokens else "NULL"
    return _ACCESS_CLAUSE.format(marks=marks), tuple(sorted(group_tokens))


#: Phrases an author writes when they are summarising what a unit taught.
#:
#: **Measured, not guessed.** Eleven candidates were run against both live
#: courses; exactly these two families fired. On OEX101 they select 4 of 55
#: blocks — `Learning Objectives` and three `Module Summary` blocks — and on
#: DemoX (227 chunks) they select none, which is the correct answer for a course
#: whose author wrote no summaries.
#:
#: Matching on the BODY rather than the title is the point. Title matching was
#: measured first and pulls in `History overview`, `Takeaways` and DemoX's
#: `Assessments Summary` — blocks that are not summaries of what was taught.
#:
#: The container noun and the finishing verb are generalised over, because those
#: are variants of the same authored sentence. Anything beyond that family must
#: be measured against real courses before it is added here; an unmeasured marker
#: silently changes which blocks a student is shown as their course overview.
_SUMMARY_MARKERS: tuple[str, ...] = (
    "in this module we learned",
    "in this section we learned",
    "in this unit we learned",
    "in this chapter we learned",
    "in this course we learned",
    "after finishing this course you",
    "after finishing this module you",
    "after completing this course you",
    "after completing this module you",
)

#: Punctuation authors vary freely inside these sentences ("we learned:" vs
#: "we learned"). Stripped before matching so a colon does not decide whether a
#: student sees their course overview.
_PUNCT = re.compile(r"[,;:!.’'\"]")


def _normalise_for_marker(text: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", (text or "").lower())).strip()


def _is_summary_text(text: str) -> bool:
    normalised = _normalise_for_marker(text)
    return any(_normalise_for_marker(m) in normalised for m in _SUMMARY_MARKERS)


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
        # WAL + busy_timeout, via the one helper both stores use. A reindex
        # writing while students read is this store's normal state, and on the
        # default journal that is a lock, not a queue.
        self._conn = sqlite_setup.connect(self.path)
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

        access_sql, access_params = _access_params(group_tokens)

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.usage_key, c.block_id, c.display_name, c.content_type,
                       c.text, c.ordinal, bm25(chunks_fts) AS raw
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                  AND c.tenant = ? AND c.offering_id = ? AND c.active = 1
                  {access_sql}
                ORDER BY raw
                LIMIT ?
                """,
                (match, tenant, offering_id, *access_params, limit),
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
        #
        # **But that reading only holds while reranking is OFF, and it is on by
        # default.** `LexicalReranker` overwrites this score with a blend —
        # 0.60·coverage + 0.15·proximity + 0.25·title — before the confidence
        # gate ever sees it (`knowledge/rerank.py`, then `ai/retrieval.py` takes
        # the max as `top_score`). So the number this function produces is what
        # the RETRIEVER means by confidence; the number the GATE compares against
        # `confidence_threshold` is the reranker's. With `rerank_enabled=False`
        # the two are the same and the sentence above is literally true.
        #
        # This is documented rather than changed because it was measured and the
        # blend is not the weaker signal: over the 28-question gold set the two
        # scorings answer the same 10 questions correctly, and the blend returns
        # 0 false answers against coverage-only's 2 — it catches the adversarial
        # q17/q18 that coverage alone answers. See config.confidence_threshold
        # for the measured relationship between the two scales.
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

    def summary_blocks(
        self,
        offering_id: str,
        *,
        tenant: str,
        group_tokens: frozenset[str] = frozenset(),
    ) -> list[StoredChunk]:
        """The author's own summary blocks for this offering, in index order.

        **This is not retrieval, and that is the point.** `search()` answers
        "which passages resemble this question" — a *selection*, which by
        construction cannot be exhaustive. A student asking what a course covers
        is asking about the course's contents, and the honest source for that is
        the overview its author wrote, not the three passages that happen to
        score highest against the words they typed.

        No query, no BM25, no reranking, no model. Selection is a fixed predicate
        over indexed text, so ten calls return the same rows in the same order —
        which is what makes the answer above this reproducible.

        **Scoping is identical to `search()` and shares its SQL.** Tenant,
        offering and `active` are in the WHERE clause, and block-level access
        comes from `_ACCESS_CLAUSE` — the same string `search()` uses, not a copy.
        A summary block is course content: a caller without the group token must
        no more see it here than through retrieval.

        `score` is set to 1.0 and means *nothing about relevance* — there is no
        query to be relevant to. It exists because `StoredChunk` requires it, and
        the outline path does not gate on confidence (there is no ranking whose
        confidence could be in question). Callers must not compare it against
        `confidence_threshold`.

        The marker test runs in Python rather than SQL because it normalises
        punctuation, which SQL `LIKE` cannot do. The scan is bounded by the
        offering and happens only on an outline question — 55 rows on OEX101,
        227 on DemoX. If a course ever makes that cost real, the fix is a
        generated column, not a looser filter.
        """
        access_sql, access_params = _access_params(group_tokens)

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.usage_key, c.block_id, c.display_name, c.content_type,
                       c.text, c.ordinal
                FROM chunks c
                WHERE c.tenant = ? AND c.offering_id = ? AND c.active = 1
                  {access_sql}
                ORDER BY c.id
                """,
                (tenant, offering_id, *access_params),
            ).fetchall()

        return [
            StoredChunk(
                usage_key=r["usage_key"],
                block_id=r["block_id"],
                display_name=r["display_name"],
                content_type=r["content_type"],
                text=r["text"],
                ordinal=r["ordinal"],
                score=1.0,
            )
            for r in rows
            if _is_summary_text(r["text"])
        ]

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

    def delete_by_prefix(self, offering_id: str, usage_key_prefix: str) -> int:
        """Delete chunks whose usage_key starts with usage_key_prefix.

        XBLOCK_DELETED (§5.4): removes matching chunks, updates chunk_count in offering_state.
        """
        with self._lock:
            ids = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM chunks WHERE offering_id=? AND usage_key LIKE ?",
                    (offering_id, usage_key_prefix + "%"),
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
        log.info("deleted %d chunks matching prefix %s from %s", len(ids), usage_key_prefix, offering_id)
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
