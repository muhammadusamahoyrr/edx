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
from coursemate_contracts.examprep import CLO, ExamPrepRequest, QuestionRecord
from coursemate_contracts.mastery import CLOMastery

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


def _weakness(clo: CLO, mastery: dict[str, CLOMastery]) -> tuple:
    """Sort key. Lower sorts first, i.e. weakest first.

    The first element separates *unknown* from *known*: unattempted outcomes lead,
    because resolving an unknown is worth more to a revision session than
    re-drilling a known weakness.
    """
    m = mastery.get(clo.clo_id)
    if m is None or m.attempts == 0:
        return (0, 0.0, 0)
    return (1, m.accuracy or 0.0, m.attempts)


def _render(clo: CLO, m: CLOMastery | None, questions: list[QuestionRecord]) -> str:
    if m is None or m.attempts == 0:
        standing = "not practised yet"
    else:
        standing = f"{m.correct}/{m.attempts} correct"

    lines = [f"\n## {clo.clo_id} — {clo.text}", f"_Your record: {standing}._", ""]
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


async def deterministic_plan(
    request: ExamPrepRequest, claims: StudentClaims
) -> AsyncIterator[StreamFrame]:
    """Yield frames for one plan. Never raises — failures become frames.

    Same signature and same contract as `ExamPrepAgent.stream`, so the transport
    layer cannot tell them apart. That is what makes the kill switch a routing
    decision rather than a code path with its own bugs.
    """
    offering_id = claims.offering_id

    if not boundary.has_exam_pack(offering_id):
        # Distinct from "nothing matched" (§5.1): no pack has been loaded, so the
        # honest message is "not ready", not "nothing found".
        yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.PREPARING)
        return

    try:
        clos = boundary.get_clos(offering_id, claims)
    except AuthorizationError as exc:
        log.warning("plan denied: %s", exc)
        yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.NOT_ENROLLED)
        return

    if not clos:
        yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.PREPARING)
        return

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

    ranked = sorted(clos, key=lambda c: _weakness(c, mastery))[:_MAX_CLOS]

    header = (
        "Here is a revision plan for this course, weakest outcome first.\n\n"
        "Every question below is a real past-paper question, quoted as printed. "
        "Nothing here is AI-generated.\n"
    )
    yield StreamFrame(type=FrameType.TOKEN, text=header)

    seen: set[str] = set()
    for clo in ranked:
        try:
            questions = boundary.search_past_questions(
                offering_id, claims, clo_id=clo.clo_id, limit=_PER_CLO
            )
        except AuthorizationError as exc:
            # Mid-plan denial means entitlement changed under us. Stop rather than
            # finish the plan from whatever was already fetched — a plan that is
            # silently short is worse than one that says it stopped.
            log.warning("plan denied mid-stream: %s", exc)
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.NOT_ENROLLED)
            return

        yield StreamFrame(type=FrameType.TOKEN, text=_render(clo, mastery.get(clo.clo_id), questions))

        for q in questions:
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
