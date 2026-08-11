"""The past-paper store — §7.6's "records, not blobs" claim, checked.

The filters are the feature. If `year_from` cannot express "only the last three
years", or `min_marks` quietly matches a question whose marks were never
extracted, then the store is a text search with extra columns and the design's
argument for structure does not hold.
"""

from __future__ import annotations

import pytest
from coursemate_contracts.examprep import CLO, ExamPrepPack, ExamType, QuestionRecord
from coursemate_service.knowledge.examprep_store import ExamPrepStore

TENANT = "default"
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"


def q(qid: str, **kw) -> QuestionRecord:
    base = dict(
        question_id=qid, tenant=TENANT, offering_id=kw.pop("offering_id", OFFERING),
        source_doc_id="final-2024.pdf", text=f"Explain {qid} in detail.",
    )
    return QuestionRecord(**{**base, **kw})


def pack(*questions, offering_id=OFFERING, clos=None) -> ExamPrepPack:
    return ExamPrepPack(
        offering_id=offering_id, tenant=TENANT,
        clos=clos or [CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee")],
        questions=list(questions),
    )


@pytest.fixture
def store(tmp_path):
    return ExamPrepStore(tmp_path / "examprep.db")


# --- loading ---------------------------------------------------------------


def test_load_reports_what_landed(store):
    """Never a bare success. A loader that reports 'ok' without a count is how
    226 blocks became 26 served."""
    counts = store.load_pack(pack(q("Q1"), q("Q2")))
    assert counts == {"questions": 2, "clos": 1, "replaced": 0}


def test_reloading_replaces_rather_than_appends(store):
    store.load_pack(pack(q("Q1"), q("Q2")))
    counts = store.load_pack(pack(q("Q3")))
    assert counts["replaced"] == 2
    assert [r.question_id for r in store.search_questions(
        tenant=TENANT, offering_id=OFFERING)] == ["Q3"]


def test_a_failed_load_leaves_the_previous_pack_intact(store, monkeypatch):
    """The atomicity claim, checked rather than asserted. Half a pack live is the
    write-then-swap failure mode arriving through a different door."""
    store.load_pack(pack(q("Q1"), q("Q2")))

    bad = pack(q("Q3"), q("Q4"))
    real = QuestionRecord.model_dump

    def explode(self, *a, **kw):
        if self.question_id == "Q4":
            raise RuntimeError("extractor died mid-pack")
        return real(self, *a, **kw)

    monkeypatch.setattr(QuestionRecord, "model_dump", explode)
    with pytest.raises(RuntimeError):
        store.load_pack(bad)
    monkeypatch.undo()

    assert sorted(r.question_id for r in store.search_questions(
        tenant=TENANT, offering_id=OFFERING)) == ["Q1", "Q2"]


def test_a_reload_does_not_leave_orphaned_fts_rows(store):
    """Deleting a content row out from under an external-content FTS index leaves
    it referencing a rowid that no longer resolves — and the failure surfaces
    later, as a corrupt-index error on an unrelated query."""
    store.load_pack(pack(q("Q1", text="Explain deadlock avoidance.")))
    store.load_pack(pack(q("Q2", text="Explain deadlock avoidance.")))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, query="deadlock")
    assert [r.question_id for r in found] == ["Q2"]


# --- isolation -------------------------------------------------------------


def test_another_offering_is_never_returned(store):
    store.load_pack(pack(q("Q1")))
    store.load_pack(pack(q("X1", offering_id=OTHER), offering_id=OTHER))
    assert [r.question_id for r in store.search_questions(
        tenant=TENANT, offering_id=OFFERING)] == ["Q1"]


def test_loading_one_offering_does_not_disturb_another(store):
    """`load_pack` replaces — scoped to its own offering. A DELETE that forgot
    the offering predicate would wipe a sibling course and report success."""
    store.load_pack(pack(q("Q1")))
    store.load_pack(pack(q("X1", offering_id=OTHER), offering_id=OTHER))
    store.load_pack(pack(q("Q2")))
    assert [r.question_id for r in store.search_questions(
        tenant=TENANT, offering_id=OTHER)] == ["X1"]


def test_another_tenant_is_never_returned(store):
    store.load_pack(pack(q("Q1")))
    assert store.search_questions(tenant="other", offering_id=OFFERING) == []


# --- the filters that are the point ---------------------------------------


def test_year_from_excludes_older_papers(store):
    store.load_pack(pack(q("OLD", year=2019), q("NEW", year=2024)))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, year_from=2023)
    assert [r.question_id for r in found] == ["NEW"]


def test_an_unknown_year_is_excluded_by_a_year_filter(store):
    """SQL three-valued logic makes this the default, and it is the right one:
    'only the last 3 years' matching a paper whose year could not be extracted
    would quietly reintroduce the outdated syllabus the filter exists to remove."""
    store.load_pack(pack(q("UNKNOWN", year=None), q("NEW", year=2024)))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, year_from=2023)
    assert [r.question_id for r in found] == ["NEW"]


def test_min_marks_excludes_unknown_marks(store):
    store.load_pack(pack(q("SMALL", marks=2), q("BIG", marks=15), q("UNKNOWN", marks=None)))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, min_marks=10)
    assert [r.question_id for r in found] == ["BIG"]


def test_filters_compose(store):
    """"Final papers, 2023 onward, worth 10+ marks" is one query, not three."""
    store.load_pack(pack(
        q("A", year=2024, marks=12, exam_type=ExamType.FINAL),
        q("B", year=2024, marks=12, exam_type=ExamType.QUIZ),
        q("C", year=2019, marks=12, exam_type=ExamType.FINAL),
        q("D", year=2024, marks=3, exam_type=ExamType.FINAL),
    ))
    found = store.search_questions(
        tenant=TENANT, offering_id=OFFERING,
        exam_type="final", year_from=2023, min_marks=10,
    )
    assert [r.question_id for r in found] == ["A"]


def test_clo_filter_selects_one_outcome(store):
    store.load_pack(pack(q("A", clo_id="CLO-1"), q("B", clo_id="CLO-2")))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, clo_id="CLO-2")
    assert [r.question_id for r in found] == ["B"]


def test_an_unfiltered_search_leads_with_the_most_recent(store):
    """A revision session with no search terms wants the newest and heaviest
    questions, not whatever SQLite happens to return first."""
    store.load_pack(pack(q("OLD", year=2019), q("NEW", year=2025), q("MID", year=2022)))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING)
    assert [r.question_id for r in found] == ["NEW", "MID", "OLD"]


# --- free text is the extra, not the primary ------------------------------


def test_free_text_still_respects_the_metadata_filters(store):
    """The dangerous version: FTS matches, filters are applied afterwards or not
    at all, and a 2019 paper reaches a student who asked for 2023 onward."""
    store.load_pack(pack(
        q("OLD", year=2019, text="Explain deadlock avoidance."),
        q("NEW", year=2024, text="Explain deadlock avoidance."),
    ))
    found = store.search_questions(
        tenant=TENANT, offering_id=OFFERING, query="deadlock", year_from=2023
    )
    assert [r.question_id for r in found] == ["NEW"]


def test_fts_operators_in_a_query_do_not_raise(store):
    """A student typing "cats AND dogs" must not hit `fts5: syntax error`. Same
    hostile-input rule as the chunk store."""
    store.load_pack(pack(q("Q1")))
    for hostile in ('cats AND dogs', 'a OR b', 'NEAR("x")', '"unclosed', "* wild"):
        assert store.search_questions(
            tenant=TENANT, offering_id=OFFERING, query=hostile
        ) is not None


# --- round-tripping --------------------------------------------------------


def test_a_record_survives_the_round_trip_intact(store):
    """Every field §7.6 requires for provenance and honest labelling."""
    original = q(
        "Q1", page=4, question_number="3(b)", year=2024, exam_type=ExamType.FINAL,
        marks=15, difficulty=0.8, topic="Concurrency", clo_id="CLO-1",
        confidence=0.55, low_confidence_flag=True,
    )
    store.load_pack(pack(original))
    got = store.get_question(tenant=TENANT, offering_id=OFFERING, question_id="Q1")
    assert got == original


def test_derived_difficulty_stays_labelled_derived(store):
    """§7.6: a derived difficulty must be labelled wherever it is shown, and a
    flag that silently flips to False is how an estimate becomes a fact."""
    store.load_pack(pack(q("Q1", difficulty=0.7)))
    got = store.get_question(tenant=TENANT, offering_id=OFFERING, question_id="Q1")
    assert got.difficulty_is_derived is True


def test_stats_report_the_soft_spots(store):
    store.load_pack(pack(
        q("A", year=2019, low_confidence_flag=True),
        q("B", year=2024, clo_id="CLO-1"),
    ))
    stats = store.stats(tenant=TENANT, offering_id=OFFERING)
    assert stats["questions"] == 2
    assert stats["low_confidence"] == 1
    assert stats["clos"] == 1
    assert (stats["earliest"], stats["latest"]) == (2019, 2024)


def test_an_unconfirmed_clo_is_loaded_but_stays_unconfirmed(store):
    """§7.3: extraction is assisted, never asserted. The store must not stamp
    confirmation on anyone's behalf."""
    store.load_pack(pack(q("Q1"), clos=[CLO(clo_id="CLO-1", text="Concurrency")]))
    assert store.clos(tenant=TENANT, offering_id=OFFERING)[0].confirmed_by is None


def test_has_pack_is_false_before_any_load(store):
    """Distinct from 'nothing matched' (§5.1) — only one of them means the
    student should come back later."""
    assert store.has_pack(tenant=TENANT, offering_id=OFFERING) is False
    store.load_pack(pack(q("Q1")))
    assert store.has_pack(tenant=TENANT, offering_id=OFFERING) is True


# --- several outcomes in one call -----------------------------------------


def test_a_list_of_clos_matches_any_of_them(store):
    """One round trip instead of N. Added after the `get_plan_context` merge:
    a planner handed every outcome at once naturally searches them together,
    and the model sent a list to a field typed `str` until this existed."""
    store.load_pack(pack(q("A", clo_id="CLO-1"), q("B", clo_id="CLO-2"),
                         q("C", clo_id="CLO-3")))
    found = store.search_questions(
        tenant=TENANT, offering_id=OFFERING, clo_id=["CLO-1", "CLO-3"]
    )
    assert sorted(r.question_id for r in found) == ["A", "C"]


def test_a_single_string_clo_still_works(store):
    """The str form is the common case and must not have regressed."""
    store.load_pack(pack(q("A", clo_id="CLO-1"), q("B", clo_id="CLO-2")))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, clo_id="CLO-2")
    assert [r.question_id for r in found] == ["B"]


def test_an_empty_clo_list_is_not_a_filter(store):
    """`clo_id=[]` must not become `IN ()`, which is a SQLite syntax error, and
    must not silently match nothing either — an empty filter is no filter."""
    store.load_pack(pack(q("A", clo_id="CLO-1"), q("B", clo_id="CLO-2")))
    found = store.search_questions(tenant=TENANT, offering_id=OFFERING, clo_id=[])
    assert sorted(r.question_id for r in found) == ["A", "B"]
