"""Past-paper question store — design §7.6.

**Records, not blobs.** The whole feature turns on this. "Practice for CLO-3, from
final papers, 2023 onward, worth 10 marks or more" is a metadata filter over
structured rows. Extract only question *text* and it becomes a semantic search
over paragraphs — which cannot express "2023 onward" at all, and answers "10
marks or more" by hoping the number appears in the prose.

So the filters below are SQL predicates on typed columns, and the free-text query
is an *optional* extra signal on top, never the primary one.

**Why not write→verify→swap.** The chunk store earns that machinery because course
ingestion arrives in batches over minutes and can be interrupted halfway. A pack
arrives as one document in one call, so a single `BEGIN IMMEDIATE` transaction
gives the identical guarantee — either the whole new pack is live or the whole old
one still is — with none of the version bookkeeping. Copying the pattern here
would be cargo cult; the property that matters is atomicity, and this has it.

Isolation is `(tenant, offering_id)` on every row and in every WHERE clause, the
same rule as the chunk store: the offering is the isolation unit, because CS-101
Fall 2026 holds a different cohort from the same course a year later.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path

from coursemate_contracts.examprep import CLO, ExamPrepPack, QuestionRecord

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exam_questions (
    id              INTEGER PRIMARY KEY,
    tenant          TEXT NOT NULL,
    offering_id     TEXT NOT NULL,
    question_id     TEXT NOT NULL,
    source_doc_id   TEXT NOT NULL,
    page            INTEGER,
    question_number TEXT,
    text            TEXT NOT NULL,
    year            INTEGER,
    exam_type       TEXT,
    marks           INTEGER,
    difficulty      REAL,
    difficulty_is_derived INTEGER NOT NULL DEFAULT 1,
    topic           TEXT,
    clo_id          TEXT,
    confidence      REAL,
    extraction_method TEXT,
    low_confidence_flag INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tenant, offering_id, question_id)
);
CREATE INDEX IF NOT EXISTS ix_eq_scope ON exam_questions(tenant, offering_id);
CREATE INDEX IF NOT EXISTS ix_eq_clo   ON exam_questions(tenant, offering_id, clo_id);

-- Mirrors exam_questions.text rather than duplicating it, same as chunks_fts.
CREATE VIRTUAL TABLE IF NOT EXISTS exam_questions_fts
    USING fts5(text, content='exam_questions', content_rowid='id',
               tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS exam_clos (
    tenant       TEXT NOT NULL,
    offering_id  TEXT NOT NULL,
    clo_id       TEXT NOT NULL,
    text         TEXT NOT NULL,
    ordinal      INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT,
    PRIMARY KEY (tenant, offering_id, clo_id)
);
"""

#: FTS5 keywords are bare words, so a student typing "cats AND dogs" would inject
#: query syntax. Same treatment as `store.py`: strip punctuation, then quote every
#: term so it is a literal rather than an operator.
_FTS_UNSAFE = re.compile(r"""[^\w\s]""", re.UNICODE)

_COLUMNS = (
    "question_id", "source_doc_id", "page", "question_number", "text", "year",
    "exam_type", "marks", "difficulty", "difficulty_is_derived", "topic",
    "clo_id", "confidence", "extraction_method", "low_confidence_flag",
)


class ExamPrepStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- loading ---------------------------------------------------------

    def load_pack(self, pack: ExamPrepPack) -> dict:
        """Replace this offering's questions and CLOs atomically.

        Returns counts rather than None so the caller can assert on what landed.
        A loader that reports success without a count is how 226 blocks became 26
        served — the failure mode this project keeps re-learning.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                old = [
                    r["id"] for r in self._conn.execute(
                        "SELECT id FROM exam_questions WHERE tenant=? AND offering_id=?",
                        (pack.tenant, pack.offering_id),
                    )
                ]
                if old:
                    marks = ",".join("?" * len(old))
                    # FTS rows first: deleting the content row out from under an
                    # external-content index leaves the index referencing a rowid
                    # that no longer resolves, and the failure surfaces later as a
                    # corrupt-index error on an unrelated query.
                    self._conn.execute(
                        f"DELETE FROM exam_questions_fts WHERE rowid IN ({marks})", old
                    )
                    self._conn.execute(
                        f"DELETE FROM exam_questions WHERE id IN ({marks})", old
                    )
                self._conn.execute(
                    "DELETE FROM exam_clos WHERE tenant=? AND offering_id=?",
                    (pack.tenant, pack.offering_id),
                )

                for ordinal, clo in enumerate(pack.clos):
                    self._conn.execute(
                        "INSERT INTO exam_clos(tenant, offering_id, clo_id, text, "
                        "ordinal, confirmed_by) VALUES (?,?,?,?,?,?)",
                        (pack.tenant, pack.offering_id, clo.clo_id, clo.text,
                         ordinal, clo.confirmed_by),
                    )

                written = 0
                for q in pack.questions:
                    row = q.model_dump()
                    cur = self._conn.execute(
                        f"""INSERT INTO exam_questions
                            (tenant, offering_id, {", ".join(_COLUMNS)})
                            VALUES (?, ?, {", ".join("?" * len(_COLUMNS))})""",
                        (pack.tenant, pack.offering_id,
                         *(row[c] for c in _COLUMNS)),
                    )
                    self._conn.execute(
                        "INSERT INTO exam_questions_fts(rowid, text) VALUES (?, ?)",
                        (cur.lastrowid, q.text),
                    )
                    written += 1
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        log.info(
            "exam pack loaded for %s: %d questions, %d CLOs (replaced %d)",
            pack.offering_id, written, len(pack.clos), len(old),
        )
        return {"questions": written, "clos": len(pack.clos), "replaced": len(old)}

    # --- reads -----------------------------------------------------------

    def has_pack(self, *, tenant: str, offering_id: str) -> bool:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM exam_questions WHERE tenant=? AND offering_id=?",
                (tenant, offering_id),
            ).fetchone()[0]
        return n > 0

    def clos(self, *, tenant: str, offering_id: str) -> list[CLO]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT clo_id, text, confirmed_by FROM exam_clos "
                "WHERE tenant=? AND offering_id=? ORDER BY ordinal, clo_id",
                (tenant, offering_id),
            ).fetchall()
        return [CLO(**dict(r)) for r in rows]

    def search_questions(
        self,
        *,
        tenant: str,
        offering_id: str,
        query: str | None = None,
        clo_id: str | list[str] | None = None,
        exam_type: str | None = None,
        year_from: int | None = None,
        min_marks: int | None = None,
        limit: int = 10,
    ) -> list[QuestionRecord]:
        """Metadata filter first, free text second.

        `year_from` and `min_marks` compare against NULL-able columns, and SQL's
        three-valued logic makes `year >= 2023` **exclude** a row whose year is
        unknown. That is the right default here — "2023 onward" asking for a paper
        whose year could not be extracted would quietly reintroduce the outdated
        syllabus the filter exists to remove — but it is a silent exclusion, so
        `unknown_year_excluded` in the caller's audit line reports it.
        """
        where = ["tenant = ?", "offering_id = ?"]
        params: list[object] = [tenant, offering_id]

        if clo_id:
            # One outcome or several. The list form exists because a planner that
            # has just been handed every outcome wants to search them together —
            # one round trip instead of N, which on a CPU model is ~26 s each.
            if isinstance(clo_id, str):
                where.append("clo_id = ?")
                params.append(clo_id)
            else:
                where.append(f"clo_id IN ({','.join('?' * len(clo_id))})")
                params.extend(clo_id)
        if exam_type:
            where.append("exam_type = ?")
            params.append(exam_type)
        if year_from is not None:
            where.append("year IS NOT NULL AND year >= ?")
            params.append(year_from)
        if min_marks is not None:
            where.append("marks IS NOT NULL AND marks >= ?")
            params.append(min_marks)

        terms = _FTS_UNSAFE.sub(" ", query or "").split()
        with self._lock:
            if terms:
                match = " OR ".join(f'"{t}"' for t in terms)
                sql = (
                    f"SELECT q.* FROM exam_questions_fts f "
                    f"JOIN exam_questions q ON q.id = f.rowid "
                    f"WHERE f.text MATCH ? AND {' AND '.join('q.' + w for w in where)} "
                    f"ORDER BY bm25(exam_questions_fts) LIMIT ?"
                )
                rows = self._conn.execute(sql, (match, *params, limit)).fetchall()
            else:
                # No query: order by recency then marks. A revision session with
                # no search terms wants the most recent and heaviest questions,
                # not whatever SQLite returns first.
                sql = (
                    f"SELECT * FROM exam_questions WHERE {' AND '.join(where)} "
                    f"ORDER BY year DESC NULLS LAST, marks DESC NULLS LAST, question_id "
                    f"LIMIT ?"
                )
                rows = self._conn.execute(sql, (*params, limit)).fetchall()

        return [self._record(r, tenant, offering_id) for r in rows]

    def get_question(
        self, *, tenant: str, offering_id: str, question_id: str
    ) -> QuestionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM exam_questions WHERE tenant=? AND offering_id=? "
                "AND question_id=?",
                (tenant, offering_id, question_id),
            ).fetchone()
        return self._record(row, tenant, offering_id) if row else None

    def stats(self, *, tenant: str, offering_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS questions, "
                "       SUM(low_confidence_flag) AS low_confidence, "
                "       COUNT(clo_id) AS tagged, "
                "       MIN(year) AS earliest, MAX(year) AS latest "
                "FROM exam_questions WHERE tenant=? AND offering_id=?",
                (tenant, offering_id),
            ).fetchone()
            clos = self._conn.execute(
                "SELECT COUNT(*) FROM exam_clos WHERE tenant=? AND offering_id=?",
                (tenant, offering_id),
            ).fetchone()[0]
        out = dict(row)
        out["low_confidence"] = out["low_confidence"] or 0
        out["clos"] = clos
        return out

    @staticmethod
    def _record(row: sqlite3.Row, tenant: str, offering_id: str) -> QuestionRecord:
        data = {k: row[k] for k in _COLUMNS}
        data["difficulty_is_derived"] = bool(data["difficulty_is_derived"])
        data["low_confidence_flag"] = bool(data["low_confidence_flag"])
        return QuestionRecord(tenant=tenant, offering_id=offering_id, **data)
