"""Merging several papers into one bank — the step `load_pack` makes mandatory.

`load_pack` REPLACES an offering's questions. A multi-paper bank therefore cannot
be built by loading papers one after another; it is built by merging first and
loading once. These tests hold down the merge, and the replacement semantics that
make it necessary.

Packs are constructed here rather than extracted from PDFs. The merge tool
operates on pack JSON, so a PDF adds nothing but a dependency — and inventing a
second exam paper to test against is exactly the thing this feature must not
teach anyone to do.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from coursemate_contracts.examprep import CLO, ExamPrepPack, QuestionRecord
from coursemate_service.knowledge.examprep_store import ExamPrepStore

ROOT = Path(__file__).resolve().parents[3]
OFFERING = "course-v1:OpenedX+OEX101+2023"
TENANT = "default"


def _load_tool():
    """Import the merger by path — it is operator tooling, not an installed module."""
    spec = importlib.util.spec_from_file_location(
        "merge_packs", ROOT / "tools" / "extract" / "merge_packs.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_packs"] = module
    spec.loader.exec_module(module)
    return module


merge_packs = _load_tool()


CLOS = [
    CLO(clo_id="CLO-1", text="Community and governance", confirmed_by="dr-lee"),
    CLO(clo_id="CLO-2", text="Named releases", confirmed_by="dr-lee"),
    CLO(clo_id="CLO-3", text="Tutor deployment", confirmed_by="dr-lee"),
]


def q(doc: str, number: str, *, marks: int | None, clo: str | None = "CLO-1") -> QuestionRecord:
    return QuestionRecord(
        question_id=f"{doc}#{number}",
        tenant=TENANT,
        offering_id=OFFERING,
        source_doc_id=doc,
        page=1,
        question_number=number,
        text=f"Question {number} from {doc}, long enough to look like a real one",
        marks=marks,
        clo_id=clo,
    )


def pack(doc: str, questions: list[QuestionRecord], *, digest: str | None = None,
         clos: list[CLO] | None = None) -> ExamPrepPack:
    return ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=CLOS if clos is None else clos,
        questions=questions,
        content_sha256=digest or (f"{doc:x<64}"[:64]),
    )


# --- lossless merging ------------------------------------------------------

def test_merging_two_papers_keeps_every_question():
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5), q("p2023.pdf", "2", marks=10)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=7)])

    merged = merge_packs.merge([a, b])

    assert len(merged.questions) == 3, "a question was dropped in the merge"
    assert {x.question_id for x in merged.questions} == {
        "p2023.pdf#1", "p2023.pdf#2", "p2024.pdf#1"
    }


def test_merging_preserves_marks_exactly():
    """The number the whole exercise turns on. A merge that loses marks is a
    merge that silently shrinks what a plan can spend."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5), q("p2023.pdf", "2", marks=10)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=7), q("p2024.pdf", "2", marks=13)])

    merged = merge_packs.merge([a, b])

    assert sum(x.marks for x in merged.questions) == 35


def test_the_same_number_on_two_papers_does_not_collide():
    """Every paper has a question 1. Provenance in the id is what keeps them apart."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])

    merged = merge_packs.merge([a, b])

    ids = [x.question_id for x in merged.questions]
    assert len(set(ids)) == 2, f"ids collided across papers: {ids}"


def test_merge_order_is_stable():
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])

    first = [x.question_id for x in merge_packs.merge([a, b]).questions]
    again = [x.question_id for x in merge_packs.merge([a, b]).questions]

    assert first == again, "the same inputs produced a different pack"


# --- duplicate source documents -------------------------------------------

def test_two_papers_sharing_a_filename_are_refused_by_name():
    """The loader would reject the colliding ids without naming the cause. The
    fix is to rename a file, which is not guessable from a list of ids."""
    a = pack("final.pdf", [q("final.pdf", "1", marks=5)])
    b = pack("final.pdf", [q("final.pdf", "1", marks=9)])

    with pytest.raises(merge_packs.MergeError) as exc:
        merge_packs.merge([a, b])

    assert "final.pdf" in str(exc.value)
    assert "rename" in str(exc.value).lower()


def test_a_repeated_id_inside_one_pack_is_still_caught():
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5), q("p2023.pdf", "1", marks=5)])

    with pytest.raises(merge_packs.MergeError) as exc:
        merge_packs.merge([a])

    assert "p2023.pdf#1" in str(exc.value)


def test_a_pack_carrying_two_documents_is_refused():
    """Merging does not compose. One `content_sha256` describes one document, so
    a two-document input would file both under the same hash — and
    merge(merge(a,b),c) would then disagree with merge(a,b,c) about a bank that
    is byte-identical."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])
    composed = merge_packs.merge([a, b])

    with pytest.raises(merge_packs.MergeError) as exc:
        merge_packs.merge([composed, pack("p2025.pdf", [q("p2025.pdf", "1", marks=5)])])

    assert "does not compose" in str(exc.value)
    assert "p2023.pdf" in str(exc.value) and "p2024.pdf" in str(exc.value)


def test_the_refusal_leaves_the_one_correct_route_open():
    """Refusing composition is only safe because merging the originals works."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])
    c = pack("p2025.pdf", [q("p2025.pdf", "1", marks=5)])

    merged = merge_packs.merge([a, b, c])

    assert len(merged.questions) == 3
    assert len(merged.content_sha256) == 64


def test_packs_from_different_offerings_are_refused():
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = a.model_copy(update={"offering_id": "course-v1:OpenedX+DemoX+DemoCourse"})

    with pytest.raises(merge_packs.MergeError):
        merge_packs.merge([a, b])


# --- the manifest hash -----------------------------------------------------

def test_the_hash_covers_the_combination_not_one_document():
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])

    merged = merge_packs.merge([a, b])

    assert merged.content_sha256 not in (a.content_sha256, b.content_sha256)
    assert len(merged.content_sha256) == 64


def test_the_same_papers_merge_to_the_same_hash():
    """So a re-run of the identical merge is still recognised as a duplicate."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])

    assert merge_packs.merge([a, b]).content_sha256 == \
        merge_packs.merge([b, a]).content_sha256


def test_adding_a_paper_changes_the_hash():
    """So the reload is not refused as a duplicate of the smaller bank."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=5)])
    c = pack("p2025.pdf", [q("p2025.pdf", "1", marks=5)])

    assert merge_packs.merge([a, b]).content_sha256 != \
        merge_packs.merge([a, b, c]).content_sha256


def test_a_source_without_a_hash_disables_the_duplicate_check_honestly():
    """Half a manifest would answer 'have we imported this' with a confident
    wrong answer. None makes the loader report duplicate_checked: false."""
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5)])
    b = ExamPrepPack(offering_id=OFFERING, tenant=TENANT, clos=CLOS,
                     questions=[q("p2024.pdf", "1", marks=5)], content_sha256=None)

    assert merge_packs.merge([a, b]).content_sha256 is None


# --- CLO coverage ----------------------------------------------------------

def test_coverage_reports_marks_per_outcome():
    merged = merge_packs.merge([pack("p.pdf", [
        q("p.pdf", "1", marks=5, clo="CLO-1"),
        q("p.pdf", "2", marks=10, clo="CLO-1"),
        q("p.pdf", "3", marks=8, clo="CLO-2"),
    ])])

    cov = merge_packs.coverage(merged)

    assert cov["per_clo"]["CLO-1"]["marks"] == 15
    assert cov["per_clo"]["CLO-2"]["marks"] == 8
    assert cov["total_marks"] == 23


def test_an_outcome_with_no_questions_is_reported_as_a_problem():
    """CLO-3's exact condition: the planner drops it and the generator abstains,
    so a bank that leaves it empty leaves the outcome unreachable."""
    merged = merge_packs.merge([pack("p.pdf", [
        q("p.pdf", "1", marks=30, clo="CLO-1"),
        q("p.pdf", "2", marks=30, clo="CLO-2"),
    ])])

    problems = merge_packs.report(merged, 70, out=io.StringIO())

    assert any("CLO-3" in p and "no questions" in p for p in problems)
    assert merge_packs.coverage(merged)["per_clo"]["CLO-3"]["questions"] == 0


def test_untagged_questions_are_reported_not_counted():
    merged = merge_packs.merge([pack("p.pdf", [
        q("p.pdf", "1", marks=5, clo=None),
        q("p.pdf", "2", marks=5, clo="CLO-1"),
    ])])

    cov = merge_packs.coverage(merged)

    assert cov["untagged"] == 1
    assert cov["per_clo"]["CLO-1"]["marks"] == 5
    assert cov["total_marks"] == 5, "an untagged question was counted as coverage"


def test_a_question_without_marks_is_flagged_as_unbudgetable():
    """`_pack` skips these, so they look like coverage and spend nothing."""
    merged = merge_packs.merge([pack("p.pdf", [
        q("p.pdf", "1", marks=None, clo="CLO-1"),
        q("p.pdf", "2", marks=5, clo="CLO-1"),
    ])])

    row = merge_packs.coverage(merged)["per_clo"]["CLO-1"]

    assert row["questions"] == 2
    assert row["marks"] == 5
    assert row["unbudgetable"] == 1


# --- replacement semantics -------------------------------------------------

def test_load_pack_replaces_rather_than_appends(tmp_path):
    """The reason merging exists. Pinned so nobody 'fixes' it into an append and
    breaks the atomic-replace guarantee the loader is built on."""
    store = ExamPrepStore(tmp_path / "e.db")
    first = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5), q("p2023.pdf", "2", marks=5)])
    store.load_pack(first)

    second = pack("p2024.pdf", [q("p2024.pdf", "1", marks=9)],
                  digest="b" * 64)
    counts = store.load_pack(second)

    assert counts["questions"] == 1
    assert counts["replaced"] == 2, "the first paper was not replaced"
    live = store.search_questions(tenant=TENANT, offering_id=OFFERING, limit=50)
    assert {x.question_id for x in live} == {"p2024.pdf#1"}, \
        "loading a second paper on its own did not destroy the first — if this " \
        "now appends, merging is no longer required and this suite needs rewriting"


def test_loading_the_merged_pack_keeps_both_papers(tmp_path):
    """The corrected workflow, end to end: merge, then load once."""
    store = ExamPrepStore(tmp_path / "e.db")
    a = pack("p2023.pdf", [q("p2023.pdf", "1", marks=5), q("p2023.pdf", "2", marks=5)])
    b = pack("p2024.pdf", [q("p2024.pdf", "1", marks=9)], digest="b" * 64)

    counts = store.load_pack(merge_packs.merge([a, b]))

    assert counts["questions"] == 3
    live = store.search_questions(tenant=TENANT, offering_id=OFFERING, limit=50)
    assert {x.question_id for x in live} == {
        "p2023.pdf#1", "p2023.pdf#2", "p2024.pdf#1"
    }
    assert sum(x.marks for x in live) == 19


# --- what the bank has to support -----------------------------------------

def test_the_current_bank_cannot_fill_a_70_mark_plan():
    """OEX101 as it stands: 35 marks over two outcomes, CLO-3 empty. Recorded as
    a test so the shortfall is a fact the suite asserts, not a note in a report."""
    from coursemate_service.ai.planner import build_plan

    questions_by_clo = {
        "CLO-1": [q("p.pdf", "1", marks=2, clo="CLO-1"),
                  q("p.pdf", "3", marks=15, clo="CLO-1"),
                  q("p.pdf", "4", marks=3, clo="CLO-1")],
        "CLO-2": [q("p.pdf", "2", marks=10, clo="CLO-2"),
                  q("p.pdf", "2(b)", marks=5, clo="CLO-2")],
    }

    plan, report = build_plan(
        offering_id=OFFERING, clos=CLOS, questions_by_clo=questions_by_clo,
        mastery={}, marks_budget=70,
    )

    assert report.planned_marks == 35
    assert not any(i.clo_id == "CLO-3" for i in plan.items), \
        "CLO-3 has no questions and must not appear in the plan"


def test_a_bank_with_enough_coverage_fills_a_70_mark_plan():
    """The acceptance condition for whatever papers get loaded: 70 requested,
    70 allocated, all three outcomes present."""
    from coursemate_service.ai.planner import build_plan

    # Mixed sizes per outcome, DOWN TO 1 mark. First-fit packing spends what
    # fits and skips what does not, so a pool whose smallest question is 2 marks
    # cannot close a share that has 1 mark left — [15,10,5,3,2] allocates 69 of
    # 70, not 70. Granularity is a coverage requirement, not a detail.
    questions_by_clo = {
        clo: [q("p.pdf", f"{clo}-{i}", marks=m, clo=clo)
              for i, m in enumerate([15, 10, 5, 3, 2, 1])]
        for clo in ("CLO-1", "CLO-2", "CLO-3")
    }

    plan, report = build_plan(
        offering_id=OFFERING, clos=CLOS, questions_by_clo=questions_by_clo,
        mastery={}, marks_budget=70,
    )

    assert report.planned_marks == 70, f"only {report.planned_marks} of 70 allocated"
    assert {i.clo_id for i in plan.items} == {"CLO-1", "CLO-2", "CLO-3"}
