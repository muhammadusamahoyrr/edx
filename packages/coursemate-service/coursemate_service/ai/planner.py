"""Study plans sized by a marks budget — §7.4.

    "a two-hour session"  ->  100 marks  ->  which outcomes, which questions

**Deterministic, and deliberately not an agent.** This is arithmetic over data the
service already has: the confirmed outcome list, the student's own mastery
counters, and past-paper questions with the marks printed on them. A model adds
nothing a weighted split cannot do, and it would add a failure mode — a plan that
quietly omits an outcome is indistinguishable from one that decided to. So there
is no model call here, no tool loop, and no provider dependency; the same
reasoning that makes `api/plan.py` the honest kill-switch target.

**Marks, not question counts.** §7.4's unit is the thing a student actually
budgets: a two-hour exam is 100 marks, and "8 questions" means nothing until you
know whether they are 2-mark or 25-mark questions. Every number below is marks.

**A question with no marks cannot be budgeted, and is not guessed at.** A fresh
pack often has them — `extract_pack.py` reports `without_marks` for exactly this
reason. Charging such a question a default would make the budget a fiction in the
one direction that matters (over-filling a student's session), so unmarked
questions are excluded and `PlanReport.unbudgetable` says how many, rather than
the plan silently being wrong.

**Weakest first, and the split follows the weakness.** The ranking rule is shared
with `api/plan.py` rather than copied — two orderings that were meant to agree and
drifted would show up as the prose plan and the budgeted plan recommending
different outcomes to the same student on the same day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.examprep import CLO, QuestionRecord, StudyPlan, StudyPlanItem
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot

from ..boundary.impl import boundary

log = logging.getLogger(__name__)

#: How many outcomes one session covers, matching `api/plan.py`. A plan longer
#: than this is a syllabus, and a student reading a syllabus is not revising.
MAX_CLOS = 5

#: Candidates fetched per outcome. More than are used, because the packer skips
#: questions that do not fit the remaining share and needs something to fall back
#: to — fetching exactly the number wanted would make a 25-mark question at the
#: top of the list cost the whole item.
#:
#: Sized so it is not the binding constraint. At 12 it was: a 60-mark share over
#: 1-mark questions could only ever spend 12, and `PlanReport` then reported "the
#: bank ran short" when the bank was fine and the *fetch* was short — a reason an
#: operator would act on and get nowhere. 40 questions on one outcome is already
#: far past a single revision session, so the cap now bounds only the pathological
#: case it exists for.
_CANDIDATES_PER_CLO = 40

#: Below this, a share cannot buy even a small question, so it is not worth
#: splitting off. The remainder rolls to the next outcome instead.
_MIN_ITEM_MARKS = 1


def weakness_key(clo: CLO, mastery: dict[str, CLOMastery]) -> tuple:
    """Sort key, weakest first. Shared with `api/plan.py`.

    The first element separates *unknown* from *known*: unattempted outcomes
    lead, because resolving an unknown is worth more to a revision session than
    re-drilling a known weakness. Treating unattempted as 0% would rank it
    identically to one the student has failed repeatedly; treating it as 100%
    would hide it entirely.
    """
    m = mastery.get(clo.clo_id)
    if m is None or m.attempts == 0:
        return (0, 0.0, 0)
    return (1, m.accuracy or 0.0, m.attempts)


def _weight(clo: CLO, mastery: dict[str, CLOMastery]) -> float:
    """Share of the budget this outcome deserves, before normalising.

    The gap between what the student can do and 100%. An unattempted outcome
    weighs a full 1.0 — the same "unknown is worth resolving" rule the sort key
    uses, applied to how much time it gets rather than to where it appears.

    Floored above zero so a fully-mastered outcome still receives something when
    it makes the cut: a plan that lists an outcome and then allocates it nothing
    is a plan with a lie in it.
    """
    m = mastery.get(clo.clo_id)
    if m is None or m.attempts == 0:
        return 1.0
    return max(0.1, 1.0 - (m.accuracy or 0.0))


@dataclass
class PlanReport:
    """Why the plan looks the way it does.

    Separate from `StudyPlan` so the contract stays unchanged: this is diagnostic
    detail for an operator and for tests, not something a student is shown. It
    exists because "empty plan" has several causes that need different responses
    — no pack loaded, no outcomes confirmed, nothing tagged, nothing with marks —
    and collapsing them into an empty list is how a fixable problem looks like a
    working feature.
    """

    requested_marks: int = 0
    planned_marks: int = 0
    clos_considered: int = 0
    clos_planned: int = 0
    questions_planned: int = 0
    #: Questions skipped because they carry no marks and so cannot be budgeted.
    unbudgetable: int = 0
    reason: str = ""

    @property
    def unspent_marks(self) -> int:
        return self.requested_marks - self.planned_marks

    def as_dict(self) -> dict:
        return {
            "requested_marks": self.requested_marks,
            "planned_marks": self.planned_marks,
            "unspent_marks": self.unspent_marks,
            "clos_considered": self.clos_considered,
            "clos_planned": self.clos_planned,
            "questions_planned": self.questions_planned,
            "unbudgetable": self.unbudgetable,
            "reason": self.reason,
        }


def _split_budget(weights: list[float], total: int) -> list[int]:
    """Whole marks per outcome, summing to exactly `total`.

    Largest-remainder rather than rounding each share independently: independent
    rounding loses or invents marks, and a 100-mark request that allocates 98 or
    103 makes the one number the student asked for untrue.
    """
    if total <= 0 or not weights:
        return [0] * len(weights)
    mass = sum(weights)
    if mass <= 0:
        return [0] * len(weights)

    exact = [total * w / mass for w in weights]
    floors = [int(x) for x in exact]
    remainder = total - sum(floors)
    # Ties broken by position, which is weakness order — so a leftover mark goes
    # to the weaker outcome. Deterministic, which matters: the same request must
    # produce the same plan twice.
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def _pack(questions: list[QuestionRecord], budget: int) -> tuple[list[QuestionRecord], int]:
    """Fill `budget` marks from `questions`, in the order given.

    First-fit over the store's own ordering (recency, then marks descending)
    rather than an optimal knapsack. Two reasons, and neither is laziness: the
    store's order is already the pedagogically useful one — recent, heavy
    questions first — and an optimal fill would happily choose six 2-mark
    questions over one 15-mark question because they total higher, which is the
    opposite of what a student revising for a final needs.

    Skips a question that does not fit rather than stopping, so a single 25-mark
    question near the top does not end an item with 24 marks unspent.
    """
    chosen: list[QuestionRecord] = []
    spent = 0
    for q in questions:
        if q.marks is None:
            continue
        if spent + q.marks <= budget:
            chosen.append(q)
            spent += q.marks
    return chosen, spent


def _rationale(clo: CLO, m: CLOMastery | None, share: int, spent: int) -> str:
    if m is None or m.attempts == 0:
        standing = "not practised yet"
    else:
        standing = f"{m.correct}/{m.attempts} correct"
    text = f"{standing}; {spent} of {share} marks allocated"
    if spent < share:
        # Said, not hidden. A student who is told the bank ran short can go find
        # more practice; one who is not will assume the plan is complete.
        text += " (bank had nothing smaller that fit)"
    return text


def build_plan(
    offering_id: str,
    clos: list[CLO],
    questions_by_clo: dict[str, list[QuestionRecord]],
    *,
    marks_budget: int,
    mastery: dict[str, CLOMastery] | None = None,
    max_clos: int = MAX_CLOS,
) -> tuple[StudyPlan, PlanReport]:
    """The whole planner, as a pure function. No I/O, no clock, no provider.

    Everything that decides what a student sees lives here so it can be tested
    exhaustively without a database — the same reason `clo_tagger._decide` is
    pure. `plan_for_offering` below is the thin part that fetches.
    """
    mastery = mastery or {}
    report = PlanReport(requested_marks=marks_budget, clos_considered=len(clos))

    if marks_budget <= 0:
        report.reason = "marks budget must be positive"
        return StudyPlan(offering_id=offering_id), report
    if not clos:
        report.reason = "no confirmed outcomes for this offering"
        return StudyPlan(offering_id=offering_id), report

    ranked = sorted(clos, key=lambda c: weakness_key(c, mastery))[:max_clos]

    # Outcomes with nothing budgetable are dropped BEFORE the split, not after.
    # Splitting first would hand a share to an outcome that cannot spend it and
    # leave the budget quietly short by that much.
    usable: list[CLO] = []
    for clo in ranked:
        pool = questions_by_clo.get(clo.clo_id, [])
        report.unbudgetable += sum(1 for q in pool if q.marks is None)
        if any(q.marks is not None for q in pool):
            usable.append(clo)

    if not usable:
        report.reason = (
            "no past-paper question with a marks value is tagged to any of the "
            "outcomes selected"
        )
        return StudyPlan(offering_id=offering_id), report

    shares = _split_budget([_weight(c, mastery) for c in usable], marks_budget)

    items: list[StudyPlanItem] = []
    carry = 0
    used: set[str] = set()
    for clo, share in zip(usable, shares, strict=True):
        share += carry
        if share < _MIN_ITEM_MARKS:
            carry = share
            continue
        # A question already used earlier in the plan is not offered twice: the
        # same question under two outcomes reads as padding, and it spends the
        # budget without adding practice.
        pool = [q for q in questions_by_clo.get(clo.clo_id, []) if q.question_id not in used]
        chosen, spent = _pack(pool, share)
        # Unspent marks roll forward rather than evaporating, so a thin outcome
        # does not shrink the whole session.
        carry = share - spent
        if not chosen:
            continue
        used.update(q.question_id for q in chosen)
        items.append(StudyPlanItem(
            clo_id=clo.clo_id,
            marks_budget=spent,
            question_ids=[q.question_id for q in chosen],
            rationale=_rationale(clo, mastery.get(clo.clo_id), share, spent),
        ))

    report.clos_planned = len(items)
    report.questions_planned = sum(len(i.question_ids) for i in items)
    report.planned_marks = sum(i.marks_budget for i in items)
    if not items:
        report.reason = "no question fit the available budget"
    elif report.unspent_marks > 0:
        report.reason = f"{report.unspent_marks} marks unspent; the bank ran short"

    return StudyPlan(offering_id=offering_id, items=items), report


def plan_for_offering(
    claims: StudentClaims,
    *,
    marks_budget: int,
    snapshot: MasterySnapshot | None = None,
    max_clos: int = MAX_CLOS,
) -> tuple[StudyPlan, PlanReport]:
    """Fetch through the boundary, then plan.

    Scope comes from the verified JWT and nothing else. There is no `offering_id`
    parameter on purpose — the same rule the request contracts follow, one level
    up: a caller cannot pass one, so a caller cannot widen one. Enrollment is
    re-derived at the boundary on every call inside this function, not asserted
    once here.
    """
    offering_id = claims.offering_id

    mastery: dict[str, CLOMastery] = {}
    if snapshot is not None and snapshot.offering_id == offering_id:
        mastery = snapshot.by_clo()
    elif snapshot is not None:
        # Browser-carried and therefore attacker-controlled. A snapshot minted for
        # another offering shapes nothing here; it is checked, not trusted.
        log.warning(
            "mastery snapshot for %s arrived on a request scoped to %s; ignoring",
            snapshot.offering_id, offering_id,
        )

    clos = boundary.get_clos(offering_id, claims)
    questions_by_clo = {
        clo.clo_id: boundary.search_past_questions(
            offering_id, claims, clo_id=clo.clo_id, limit=_CANDIDATES_PER_CLO
        )
        for clo in sorted(clos, key=lambda c: weakness_key(c, mastery))[:max_clos]
    }
    return build_plan(
        offering_id, clos, questions_by_clo,
        marks_budget=marks_budget, mastery=mastery, max_clos=max_clos,
    )
