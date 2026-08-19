"""The budgeted study planner — §7.4.

The budget is the feature. "A two-hour session" means 100 marks, and a plan that
allocates 103 has told the student something untrue about the one number they
gave it. So most of what follows is arithmetic held to an exact promise:

    sum(item.marks_budget) <= marks_budget, always, with no exception

The rest is about the empty cases. An empty plan has four different causes — no
outcomes, nothing tagged, nothing with marks, budget too small — and they need
different responses from whoever is looking at it, so the planner reports which
rather than returning a bare empty list.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.examprep import CLO, QuestionRecord
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
from coursemate_service.ai.planner import (
    PlanReport,
    build_plan,
    plan_for_offering,
    weakness_key,
)

OFFERING = "course-v1:OpenedX+OEX101+2024"
OTHER = "course-v1:OpenedX+OEX101+2023"
TENANT = "default"


def clo(cid: str, text: str = "an outcome") -> CLO:
    return CLO(clo_id=cid, text=text, confirmed_by="dr-lee")


def q(qid: str, marks: int | None, clo_id: str = "CLO-1") -> QuestionRecord:
    return QuestionRecord(
        question_id=qid, tenant=TENANT, offering_id=OFFERING,
        source_doc_id="final-2024.pdf", text=f"Question {qid}",
        marks=marks, clo_id=clo_id,
    )


def mastery(**by_id: tuple[int, int]) -> dict[str, CLOMastery]:
    """`CLO-1=(attempts, correct)`."""
    return {
        cid: CLOMastery(clo_id=cid, attempts=a, correct=c)
        for cid, (a, c) in by_id.items()
    }


def total(plan) -> int:
    return sum(i.marks_budget for i in plan.items)


# --- the shortfall the student is shown -------------------------------------


def test_the_plan_states_what_was_asked_for_and_what_was_filled():
    """The browser used to subtract the items from the budget it had sent. Those
    are the planner's numbers, so the planner states them."""
    clos = [clo("CLO-1")]
    bank = {"CLO-1": [q("Q1", 20, "CLO-1")]}

    plan, report = build_plan(OFFERING, clos, bank, marks_budget=70)

    assert plan.requested_marks == 70
    assert plan.planned_marks == 20
    assert plan.planned_marks == total(plan)


def test_the_contract_and_the_report_never_disagree():
    """Both carry the same two numbers. Set independently they drift, and the
    student and the operator end up reading different plans."""
    clos = [clo(f"CLO-{i}") for i in range(1, 4)]
    bank = {
        f"CLO-{i}": [q(f"Q{i}-{j}", m, f"CLO-{i}") for j, m in enumerate((15, 10, 5, 3, 2, 1))]
        for i in range(1, 4)
    }
    for budget in (1, 7, 35, 70, 200, 500):
        plan, report = build_plan(OFFERING, clos, bank, marks_budget=budget)
        assert plan.requested_marks == report.requested_marks
        assert plan.planned_marks == report.planned_marks


def test_an_unfillable_plan_still_states_the_request():
    """The empty cases are exactly where a client cannot re-derive the number:
    there are no items to sum, so a shortfall derived from them reads as zero."""
    plan, report = build_plan(OFFERING, [], {}, marks_budget=70)

    assert plan.items == []
    assert plan.requested_marks == 70
    assert plan.planned_marks == 0


def test_allocation_is_unchanged_by_carrying_the_numbers():
    """The fix reports; it does not plan. Same bank, same budget, same items."""
    clos = [clo("CLO-1"), clo("CLO-2")]
    bank = {
        "CLO-1": [q("Q1", 15, "CLO-1"), q("Q2", 5, "CLO-1")],
        "CLO-2": [q("Q3", 10, "CLO-2")],
    }

    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=70)

    assert [i.clo_id for i in plan.items] == ["CLO-1", "CLO-2"]
    assert [i.question_ids for i in plan.items] == [["Q1", "Q2"], ["Q3"]]
    assert total(plan) == 30


# --- the budget promise ----------------------------------------------------


@pytest.mark.parametrize("budget", [1, 5, 10, 25, 37, 100, 101, 250])
def test_a_plan_never_exceeds_its_budget(budget):
    """The invariant the whole feature rests on, over a bank whose marks do not
    divide evenly into anything."""
    clos = [clo(f"CLO-{i}") for i in range(1, 5)]
    bank = {
        f"CLO-{i}": [q(f"Q{i}-{j}", m, f"CLO-{i}") for j, m in enumerate((15, 10, 7, 3, 2))]
        for i in range(1, 5)
    }
    plan, report = build_plan(OFFERING, clos, bank, marks_budget=budget)

    assert total(plan) <= budget
    assert report.planned_marks == total(plan)
    assert report.unspent_marks >= 0


def test_the_budget_is_spent_in_marks_not_in_questions():
    """§7.4's unit. "8 questions" means nothing until you know whether they are
    2-mark or 25-mark questions, so a 30-mark budget buys two 15s, not eight of
    whatever came first."""
    bank = {"CLO-1": [q("A", 15), q("B", 15), q("C", 15)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=30)

    assert total(plan) == 30
    assert plan.items[0].question_ids == ["A", "B"]


def test_a_question_that_does_not_fit_is_skipped_not_stopped_on():
    """A single heavy question near the top must not end the item with the rest
    of the budget unspent."""
    bank = {"CLO-1": [q("BIG", 25), q("A", 5), q("B", 4)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=10)

    assert plan.items[0].question_ids == ["A", "B"]
    assert total(plan) == 9


def test_the_split_loses_no_marks_to_rounding():
    """Largest-remainder, not independent rounding. Three equal outcomes over 100
    marks is 34/33/33, never 33/33/33 with a mark quietly dropped."""
    clos = [clo(f"CLO-{i}") for i in (1, 2, 3)]
    bank = {f"CLO-{i}": [q(f"Q{i}-{j}", 1, f"CLO-{i}") for j in range(50)] for i in (1, 2, 3)}
    plan, report = build_plan(OFFERING, clos, bank, marks_budget=100)

    assert total(plan) == 100
    assert report.unspent_marks == 0


def test_the_same_request_produces_the_same_plan_twice():
    """Determinism is a requirement, not a nicety: a student who reloads must not
    get a different plan, and a planner nobody can reproduce cannot be debugged."""
    clos = [clo("CLO-1"), clo("CLO-2"), clo("CLO-3")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 5, c.clo_id) for j in range(6)] for c in clos}
    m = mastery(**{"CLO-1": (10, 3), "CLO-2": (10, 9)})

    first, _ = build_plan(OFFERING, clos, bank, marks_budget=40, mastery=m)
    second, _ = build_plan(OFFERING, clos, bank, marks_budget=40, mastery=m)
    assert first == second


def test_unspent_marks_roll_to_the_next_outcome():
    """A thin outcome must not shrink the whole session. CLO-1 can spend only 2
    of its share; the rest has to reach CLO-2 rather than evaporating."""
    clos = [clo("CLO-1"), clo("CLO-2")]
    bank = {"CLO-1": [q("A", 2)], "CLO-2": [q("B", 10), q("C", 10), q("D", 10)]}
    plan, report = build_plan(OFFERING, clos, bank, marks_budget=40)

    assert total(plan) > 12, "CLO-2 should have absorbed CLO-1's leftover"
    assert report.planned_marks == total(plan)


def test_a_share_too_small_to_buy_anything_rolls_on_rather_than_making_an_item():
    """Four outcomes over three marks: one share rounds to nothing. An item with
    a zero budget and no questions is noise in the plan, so the share moves on
    instead."""
    clos = [clo(f"CLO-{i}") for i in range(1, 5)]
    bank = {c.clo_id: [q(f"{c.clo_id}-a", 1, c.clo_id)] for c in clos}
    plan, report = build_plan(OFFERING, clos, bank, marks_budget=3)

    assert total(plan) == 3
    assert len(plan.items) == 3
    assert all(i.marks_budget > 0 and i.question_ids for i in plan.items)
    assert report.clos_planned == 3


def test_a_question_is_never_offered_under_two_outcomes():
    """The same question twice reads as padding, and it spends budget without
    adding practice."""
    clos = [clo("CLO-1"), clo("CLO-2")]
    shared = [q("SHARED", 10), q("A", 10)]
    plan, _ = build_plan(OFFERING, clos, {"CLO-1": shared, "CLO-2": shared},
                         marks_budget=100)

    ids = [qid for item in plan.items for qid in item.question_ids]
    assert len(ids) == len(set(ids))


# --- CLO and mastery integration -------------------------------------------


def test_the_weakest_outcome_is_planned_first():
    clos = [clo("STRONG"), clo("WEAK")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 5, c.clo_id) for j in range(4)] for c in clos}
    m = mastery(STRONG=(10, 9), WEAK=(10, 2))
    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=40, mastery=m)

    assert plan.items[0].clo_id == "WEAK"


def test_an_unattempted_outcome_outranks_a_merely_weak_one():
    """Unknown is worth resolving. Treating unattempted as 0% would rank it with
    an outcome the student has failed repeatedly; treating it as 100% would hide
    it — the same rule `CLOMastery.accuracy` follows by returning None."""
    clos = [clo("FAILED"), clo("UNTRIED")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 5, c.clo_id) for j in range(4)] for c in clos}
    m = mastery(FAILED=(10, 1))
    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=40, mastery=m)

    assert plan.items[0].clo_id == "UNTRIED"


def test_a_weaker_outcome_gets_a_larger_share():
    clos = [clo("WEAK"), clo("STRONG")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 1, c.clo_id) for j in range(100)] for c in clos}
    m = mastery(WEAK=(10, 1), STRONG=(10, 9))
    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=100, mastery=m)

    by_clo = {i.clo_id: i.marks_budget for i in plan.items}
    assert by_clo["WEAK"] > by_clo["STRONG"]


def test_a_mastered_outcome_that_makes_the_cut_still_gets_marks():
    """A plan that lists an outcome and allocates it nothing is a plan with a lie
    in it."""
    clos = [clo("PERFECT")]
    bank = {"PERFECT": [q("A", 5), q("B", 5)]}
    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=20,
                         mastery=mastery(PERFECT=(10, 10)))

    assert plan.items and plan.items[0].marks_budget > 0


def test_no_mastery_at_all_is_a_legitimate_state():
    """A new student. Not an error, and not a reason to plan nothing."""
    bank = {"CLO-1": [q("A", 10)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=20)

    assert plan.items[0].question_ids == ["A"]
    assert report.clos_planned == 1


def test_the_plan_is_capped_at_five_outcomes():
    """A plan longer than this is a syllabus, and a student reading a syllabus is
    not revising."""
    clos = [clo(f"CLO-{i}") for i in range(1, 12)]
    bank = {c.clo_id: [q(f"{c.clo_id}-a", 10, c.clo_id)] for c in clos}
    plan, _ = build_plan(OFFERING, clos, bank, marks_budget=500)

    assert len(plan.items) <= 5


def test_the_rationale_reports_the_student_standing_and_the_allocation():
    bank = {"CLO-1": [q("A", 10)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=10,
                         mastery=mastery(**{"CLO-1": (8, 5)}))

    assert "5/8 self-marked" in plan.items[0].rationale
    assert "10 of 10 marks" in plan.items[0].rationale


# --- empty and insufficient banks ------------------------------------------


def test_no_outcomes_is_reported_not_returned_as_a_blank_plan():
    plan, report = build_plan(OFFERING, [], {}, marks_budget=100)

    assert plan.items == []
    assert "no confirmed outcomes" in report.reason


def test_an_untagged_bank_says_so():
    """Questions exist, none is tagged to a planned outcome. Different from "no
    pack", and it needs a different fix — run the CLO tagger."""
    plan, report = build_plan(OFFERING, [clo("CLO-1")], {}, marks_budget=100)

    assert plan.items == []
    assert "marks value" in report.reason


def test_a_bank_with_no_marks_cannot_be_budgeted_and_says_how_many():
    """A fresh pack often has them — `extract_pack.py` reports `without_marks`
    for exactly this reason. Charging a default would make the budget a fiction
    in the direction that over-fills a student's session."""
    bank = {"CLO-1": [q("A", None), q("B", None), q("C", None)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=100)

    assert plan.items == []
    assert report.unbudgetable == 3
    assert "marks value" in report.reason


def test_unmarked_questions_are_skipped_but_marked_ones_still_plan():
    bank = {"CLO-1": [q("NOMARKS", None), q("A", 10), q("ALSONONE", None)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=20)

    assert plan.items[0].question_ids == ["A"]
    assert report.unbudgetable == 2


def test_a_budget_too_small_for_anything_plans_nothing_and_explains():
    bank = {"CLO-1": [q("BIG", 50)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=5)

    assert plan.items == []
    assert "fit the available budget" in report.reason


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_a_non_positive_budget_is_refused(budget):
    bank = {"CLO-1": [q("A", 10)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=budget)

    assert plan.items == []
    assert "must be positive" in report.reason


def test_a_short_bank_reports_what_it_could_not_spend():
    bank = {"CLO-1": [q("A", 10)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=100)

    assert total(plan) == 10
    assert report.unspent_marks == 90
    assert "unspent" in report.reason
    # The one question was taken, so the pool is empty — not "nothing smaller".
    assert "no more past-paper questions are tagged" in plan.items[0].rationale


# --- the shortfall clause names the actual cause ---------------------------
#
# One condition (`spent < share`) used to produce one message, and it described
# only one of the two ways a share goes unspent. On the live course it was the
# wrong one: both CLO-1 questions were already used, so there was nothing
# smaller because there was nothing at all, and the student was sent looking for
# questions that do not exist.


def test_an_exhausted_pool_says_there_are_no_more_questions():
    bank = {"CLO-1": [q("A", 3), q("B", 2)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=50)

    rationale = plan.items[0].rationale
    assert "no more past-paper questions are tagged" in rationale
    assert "smaller" not in rationale, "sent the student after questions that do not exist"


def test_a_pool_of_oversized_questions_says_they_do_not_fit():
    # 5 marks of budget: the 3-mark question is taken, 2 marks go unspent, and a
    # 10-mark question is still sitting in the pool unable to fit.
    bank = {"CLO-1": [q("A", 3), q("B", 10)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=5)

    rationale = plan.items[0].rationale
    assert "larger than the marks left" in rationale
    assert "no more past-paper questions" not in rationale


def test_an_exact_fit_explains_nothing():
    bank = {"CLO-1": [q("A", 10)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=10)

    rationale = plan.items[0].rationale
    assert rationale.endswith("10 of 10 marks allocated"), rationale


def test_the_record_is_called_self_marked_not_correct():
    """The counter is built from the student pressing "I got this".

    No answer key exists anywhere in the system, so nothing has verified any of
    it. "correct" asserts a verification that never happened and reads as a
    grade; "self-marked" says what the number actually is.
    """
    bank = {"CLO-1": [q("A", 10)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=10,
                         mastery=mastery(**{"CLO-1": (8, 5)}))

    rationale = plan.items[0].rationale
    assert "5/8 self-marked" in rationale
    assert "correct" not in rationale, "a self-report is being presented as a graded result"


def test_an_unpractised_outcome_is_unchanged():
    """"not practised yet" was already honest and stays exactly as it was."""
    bank = {"CLO-1": [q("A", 10)]}
    plan, _ = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=10)

    assert plan.items[0].rationale.startswith("not practised yet;")


def test_an_unbudgetable_leftover_is_not_offered_as_a_smaller_question():
    """A question with no marks cannot be packed, so it is not "left over".

    Calling the outcome "oversized" because an unmarked question remains would
    point the student at something the planner can never use. `unbudgetable`
    reports those separately.
    """
    bank = {"CLO-1": [q("A", 3), q("B", None)]}
    plan, report = build_plan(OFFERING, [clo("CLO-1")], bank, marks_budget=50)

    assert report.unbudgetable == 1
    assert "no more past-paper questions are tagged" in plan.items[0].rationale


def test_the_report_shape_is_stable():
    assert set(PlanReport().as_dict()) == {
        "requested_marks", "planned_marks", "unspent_marks", "clos_considered",
        "clos_planned", "questions_planned", "unbudgetable", "reason",
    }


# --- the shared ranking rule -----------------------------------------------


def test_the_prose_planner_and_the_budgeted_planner_rank_identically():
    """`api/plan.py` imports this key rather than keeping its own. Two orderings
    meant to agree would drift, and the drift would show up as the two plans
    recommending different outcomes to the same student on the same day."""
    from coursemate_service.api import plan as prose_plan

    assert prose_plan.weakness_key is weakness_key


# --- scope, through the boundary -------------------------------------------


class _Boundary:
    """Records what scope it was asked for, and refuses anything else."""

    def __init__(self, clos, questions, allowed=OFFERING):
        self._clos, self._questions, self._allowed = clos, questions, allowed
        self.asked: list[str] = []

    def get_clos(self, offering_id, claims):
        self.asked.append(offering_id)
        self._check(offering_id, claims)
        return self._clos

    def search_past_questions(self, offering_id, claims, *, clo_id=None, limit=10, **kw):
        self.asked.append(offering_id)
        self._check(offering_id, claims)
        return self._questions.get(clo_id, [])[:limit]

    def _check(self, offering_id, claims):
        from coursemate_service.boundary.impl import AuthorizationError

        if offering_id != self._allowed or claims.offering_id != self._allowed:
            raise AuthorizationError(f"not enrolled in {offering_id}")


def _claims(offering_id=OFFERING, sub="student-1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, username="alice", course_id=offering_id, offering_id=offering_id,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


@pytest.fixture
def fake_boundary(monkeypatch):
    def install(clos, questions, allowed=OFFERING):
        b = _Boundary(clos, questions, allowed)
        from coursemate_service.ai import planner

        monkeypatch.setattr(planner, "boundary", b)
        return b

    return install


def test_a_real_offering_produces_a_budgeted_plan(fake_boundary):
    """The definition of done, end to end through the boundary."""
    clos = [clo("CLO-1", "Community"), clo("CLO-2", "Releases")]
    bank = {
        "CLO-1": [q("Q1", 15, "CLO-1"), q("Q4", 3, "CLO-1")],
        "CLO-2": [q("Q2", 10, "CLO-2"), q("Q3", 5, "CLO-2")],
    }
    fake_boundary(clos, bank)

    plan, report = plan_for_offering(_claims(), marks_budget=30)

    assert plan.offering_id == OFFERING
    assert total(plan) <= 30
    assert report.questions_planned > 0
    assert {i.clo_id for i in plan.items} <= {"CLO-1", "CLO-2"}


def test_scope_comes_from_the_token_and_nothing_else(fake_boundary):
    """There is no `offering_id` parameter, so a caller cannot pass one and a
    caller cannot widen one — the same rule the request contracts follow."""
    b = fake_boundary([clo("CLO-1")], {"CLO-1": [q("A", 10)]})
    plan_for_offering(_claims(), marks_budget=20)

    assert set(b.asked) == {OFFERING}


def test_a_caller_scoped_to_another_offering_is_refused(fake_boundary):
    """Enrollment is re-derived at the boundary on every call, not asserted once
    in the planner."""
    from coursemate_service.boundary.impl import AuthorizationError

    fake_boundary([clo("CLO-1")], {"CLO-1": [q("A", 10)]}, allowed=OFFERING)

    with pytest.raises(AuthorizationError):
        plan_for_offering(_claims(offering_id=OTHER), marks_budget=20)


def test_a_mastery_snapshot_for_another_offering_is_ignored(fake_boundary):
    """Browser-carried and therefore attacker-controlled. A snapshot minted
    elsewhere must shape nothing here."""
    clos = [clo("CLO-1"), clo("CLO-2")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 1, c.clo_id) for j in range(60)] for c in clos}
    fake_boundary(clos, bank)

    forged = MasterySnapshot(
        offering_id=OTHER,
        clos=[CLOMastery(clo_id="CLO-1", attempts=99, correct=99)],
    )
    with_forged, _ = plan_for_offering(_claims(), marks_budget=60, snapshot=forged)
    with_none, _ = plan_for_offering(_claims(), marks_budget=60)

    assert with_forged == with_none


def test_a_matching_snapshot_does_shape_the_plan(fake_boundary):
    clos = [clo("CLO-1"), clo("CLO-2")]
    bank = {c.clo_id: [q(f"{c.clo_id}-{j}", 5, c.clo_id) for j in range(20)] for c in clos}
    fake_boundary(clos, bank)

    snapshot = MasterySnapshot(
        offering_id=OFFERING,
        clos=[CLOMastery(clo_id="CLO-1", attempts=10, correct=10)],
    )
    plan, _ = plan_for_offering(_claims(), marks_budget=40, snapshot=snapshot)

    by_clo = {i.clo_id: i.marks_budget for i in plan.items}
    assert by_clo["CLO-2"] > by_clo["CLO-1"], "the mastered outcome should get less"


def test_the_candidate_fetch_is_not_the_binding_constraint(fake_boundary):
    """A cap that silently limits how much of a budget an outcome can absorb is
    worse than a low limit: `PlanReport` reports "the bank ran short" when the
    bank was fine and the *fetch* was short, which is a reason an operator would
    act on and get nowhere. Found by a weighting test that could not distinguish
    two outcomes because both had been truncated to the same size."""
    bank = {"CLO-1": [q(f"Q{j}", 1, "CLO-1") for j in range(200)]}
    fake_boundary([clo("CLO-1")], bank)

    plan, report = plan_for_offering(_claims(), marks_budget=40)

    assert total(plan) == 40
    assert report.unspent_marks == 0, "the fetch limit, not the bank, would cause this"
