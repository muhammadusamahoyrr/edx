"""The deterministic study plan — what runs with the agent switched off.

**Not a stub.** With `agent_enabled=False` — the default — this is the exam-prep
feature. It answers the request students actually make ("what should I practise")
by ranking learning outcomes against the student's own mastery and attaching real
past-paper questions to each one.

No model call, no tool loop, no provider dependency. It is a few SQL queries and a
sort, so it returns in milliseconds and cannot hallucinate: every question it shows
is a verbatim past-paper question with its source and year attached.

**This exists so the kill switch is honest.** A flag that routes to a broken or
empty path is a flag nobody will ever dare turn off, which makes it useless as a
control. It is also the baseline the agent has to beat — `eval/feature_b_rubric.py`
scores both, and "the agent is better" should be a number rather than an
assumption. The agent's advantage is meant to be the open-ended request ("a
two-hour plan weighted toward what I keep getting wrong"), not this one.

Ranking rule, deliberately simple and deliberately not a model:

    weakest first  =  unattempted CLOs, then lowest accuracy, then fewest attempts

Unattempted sorts first rather than last. A CLO with no attempts is *unknown*, and
unknown is exactly what a revision session should resolve — treating it as 0%
mastery would rank it identically to one the student has failed repeatedly, and
treating it as 100% would hide it entirely.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import Citation, FrameType, StreamFrame
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import (
    ExamPrepRequest,
    RevisionPlan,
    RevisionPlanOutcome,
)
from coursemate_contracts.mastery import CLOMastery

from ..ai.planner import weakness_key
from ..boundary.impl import AuthorizationError, boundary

log = logging.getLogger(__name__)

#: Reported on the DONE frame so a trace says which path answered, without the
#: transport layer having to know the agent package exists.
PROVIDER_NAME = "deterministic"

#: How many outcomes one session covers. A plan longer than this is a syllabus,
#: and a student reading a syllabus is not revising.
_MAX_CLOS = 5
#: Questions attached per outcome.
_PER_CLO = 3


def _render_outcome(outcome: RevisionPlanOutcome) -> str:
    """One outcome as markdown, for the STREAMING path only.

    Kept because `/plan` still streams when the agent is on, and because the
    agent-off stream is the kill switch §nobody-dares-use depends on. It now
    renders a `RevisionPlanOutcome` rather than doing its own lookups, so the
    prose and the JSON cannot disagree about what to revise.

    The markup here is exactly what the browser's renderer handles, and
    `test_plan_markup_is_renderable.py` is what keeps that true.
    """
    if outcome.attempts == 0:
        standing = "not practised yet"
    else:
        # See `planner._rationale`: the counter is the student's own self-report,
        # so calling it "correct" claims a verification that never happened.
        standing = f"{outcome.correct}/{outcome.attempts} self-marked"

    questions = outcome.questions
    lines = [f"\n## {outcome.clo_id} — {outcome.clo_text}",
             f"_Your record: {standing}._", ""]
    if not questions:
        lines.append("No past-paper question is tagged to this outcome yet.")
        return "\n".join(lines) + "\n"

    for q in questions:
        bits = []
        if q.marks is not None:
            bits.append(f"{q.marks} marks")
        if q.year is not None:
            bits.append(str(q.year))
        if q.exam_type is not None:
            bits.append(q.exam_type)
        meta = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {q.text}{meta}")
        # Provenance on every item (§7.6). A practice question a student cannot
        # trace back to a real paper is indistinguishable from one we invented.
        lines.append(f"  _Source: {q.source_doc_id}"
                     + (f", p.{q.page}" if q.page is not None else "") + "_")
        if q.low_confidence_flag:
            # Shown, not hidden. A student who knows an item was hard to extract
            # can discount it; one who does not will assume it is exact.
            lines.append("  _Extraction confidence was low — check the original._")
    return "\n".join(lines) + "\n"


class PlanUnavailable(Exception):
    """The plan cannot be built, carrying the code the caller should report.

    An exception rather than a sentinel because there are three distinct
    reasons — no pack, no CLOs, entitlement withdrawn — and both callers have to
    tell them apart: §5.1 keeps "still being prepared" and "not enrolled"
    separate all the way to the student.
    """

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def build_revision_plan(
    request: ExamPrepRequest, claims: StudentClaims
) -> RevisionPlan:
    """Select and order the plan. **The only place that decision is made.**

    Both presentations are built from this: the JSON route returns it, and
    `deterministic_plan` renders its prose from it. Two selections meant to
    agree would drift, and the drift would surface as the same student being
    told to revise different outcomes depending on which surface they opened —
    exactly the failure `weakness_key` is shared to prevent one level down.

    Synchronous: it is a handful of SQL reads and a sort, no model call. The
    callers decide whether that needs a worker thread.
    """
    offering_id = claims.offering_id

    if not boundary.has_exam_pack(offering_id):
        # Distinct from "nothing matched" (§5.1): no pack has been loaded, so the
        # honest message is "not ready", not "nothing found".
        raise PlanUnavailable(ErrorCode.PREPARING)

    try:
        clos = boundary.get_clos(offering_id, claims)
    except AuthorizationError as exc:
        log.warning("plan denied: %s", exc)
        raise PlanUnavailable(ErrorCode.NOT_ENROLLED) from exc

    if not clos:
        raise PlanUnavailable(ErrorCode.PREPARING)

    mastery: dict[str, CLOMastery] = {}
    snapshot = request.mastery
    # A snapshot minted for another offering shapes nothing here. The payload is
    # browser-carried and therefore attacker-controlled; it is checked, not trusted.
    if snapshot is not None and snapshot.offering_id == offering_id:
        mastery = snapshot.by_clo()
    elif snapshot is not None:
        log.warning(
            "mastery snapshot for %s arrived on a request scoped to %s; ignoring",
            snapshot.offering_id, offering_id,
        )

    # The same key the budgeted planner uses. Two orderings meant to agree would
    # drift, and the drift would show up as this plan and a budgeted one
    # recommending different outcomes to the same student on the same day.
    ranked = sorted(clos, key=lambda c: weakness_key(c, mastery))[:_MAX_CLOS]

    outcomes: list[RevisionPlanOutcome] = []
    for clo in ranked:
        try:
            questions = boundary.search_past_questions(
                offering_id, claims, clo_id=clo.clo_id, limit=_PER_CLO
            )
        except AuthorizationError as exc:
            # Mid-plan denial means entitlement changed under us. Stop rather than
            # finish the plan from whatever was already fetched — a plan that is
            # silently short is worse than one that says it stopped.
            log.warning("plan denied mid-build: %s", exc)
            raise PlanUnavailable(ErrorCode.NOT_ENROLLED) from exc

        m = mastery.get(clo.clo_id)
        outcomes.append(RevisionPlanOutcome(
            clo_id=clo.clo_id,
            clo_text=clo.text,
            attempts=m.attempts if m else 0,
            correct=m.correct if m else 0,
            questions=questions,
        ))

    return RevisionPlan(offering_id=offering_id, outcomes=outcomes)


async def deterministic_plan(
    request: ExamPrepRequest, claims: StudentClaims
) -> AsyncIterator[StreamFrame]:
    """Yield frames for one plan. Never raises — failures become frames.

    Same signature and same contract as `ExamPrepAgent.stream`, so the transport
    layer cannot tell them apart. That is what makes the kill switch a routing
    decision rather than a code path with its own bugs.

    **Now renders `build_revision_plan`'s output rather than selecting its own.**
    The browser prefers the structured route when the agent is off, so this path
    is the kill-switch fallback and the agent-on shape — but it must still agree
    with the JSON one about what to revise, and the only way to guarantee that
    is to share the selection rather than the intention to match.
    """
    try:
        plan = build_revision_plan(request, claims)
    except PlanUnavailable as exc:
        yield StreamFrame(type=FrameType.ERROR, error_code=exc.code)
        return

    header = (
        "Here is a revision plan for this course, weakest outcome first.\n\n"
        "Every question below is a real past-paper question, quoted as printed. "
        "Nothing here is AI-generated.\n"
    )
    yield StreamFrame(type=FrameType.TOKEN, text=header)

    seen: set[str] = set()
    for outcome in plan.outcomes:
        yield StreamFrame(type=FrameType.TOKEN, text=_render_outcome(outcome))

        for q in outcome.questions:
            if q.source_doc_id not in seen:
                seen.add(q.source_doc_id)
                # `usage_key` is the paper, not a courseware block, so no deep
                # link is offered. A `url` that resolved to nothing would be
                # worse than none — §11.2b needs a citation a rater can check.
                yield StreamFrame(
                    type=FrameType.CITATION,
                    citation=Citation(usage_key=q.source_doc_id, display_name=q.source_doc_id),
                )

    yield StreamFrame(type=FrameType.DONE, provider=PROVIDER_NAME)
