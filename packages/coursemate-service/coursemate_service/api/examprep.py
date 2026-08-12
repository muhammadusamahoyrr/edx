"""Exam prep — Feature B's student-facing endpoint (§7, §9.0).

**The kill switch is read here, not inside the agent** (decision 1). That is the
whole reason it works: with `agent_enabled=False` the deterministic path is
reached and no code in `coursemate_service.agents` executes at all. A flag checked
*inside* the agent would be an agent that starts, allocates, and then declines —
which is a slower way to fail and a much easier one to get subtly wrong.

The deterministic path is not a stub. It answers the common request — "what should
I practise" — from the same gated boundary the agent uses, by ranking CLOs against
mastery. No model call, no tool loop, ~10 ms. It is what ships on a default
install, and it is the thing the agent has to beat.

Transport only, like `chat.py`: this file authenticates, rate-limits and SSE-encodes.
What to say is decided by `agents/runner.py` or by `plan.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import StreamFrame
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import (
    CLOOption,
    ExamPrepRequest,
    ExamPrepStatus,
    PracticeRequest,
    StudyPlan,
    StudyPlanRequest,
)
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..ai.planner import plan_for_offering
from ..boundary.impl import AuthorizationError, boundary
from ..config import settings
from .deps import holding_stream_slot, rate_limited, rate_limiter

log = logging.getLogger(__name__)

router = APIRouter()


def _sse(frame: StreamFrame) -> str:
    return f"data: {frame.model_dump_json(exclude_none=True)}\n\n"


async def _encode(source: AsyncIterator[StreamFrame]) -> AsyncIterator[str]:
    async for frame in source:
        yield _sse(frame)


def _encode_holding_slot(
    source: AsyncIterator[StreamFrame], student_id: str, token: str
) -> AsyncIterator[str]:
    """`_encode`, but the concurrency slot is given back when the stream ends.

    The release policy itself moved to `deps.holding_stream_slot` when `/chat`
    gained a slot too — a second `finally` would have been a second copy of the
    rule about when a slot is safe to give back, and those drift. This stays as
    the exam-prep spelling of it: encode, then hold.
    """
    return holding_stream_slot(_encode(source), student_id, token)


@router.get("/status")
async def status(claims: StudentClaims = Depends(rate_limited)) -> ExamPrepStatus:
    """What the tab can offer, before the student asks for anything.

    Cheap and non-streaming so the tab renders its true state immediately rather
    than an enabled-looking control that turns out to do nothing (§5.1).
    """
    offering_id = claims.offering_id
    if not boundary.has_exam_pack(offering_id):
        return ExamPrepStatus(
            agent_available=settings.agent_enabled, pack_loaded=False
        )

    # The selector's options. Fetched here rather than behind a second endpoint:
    # the tab already calls /status before enabling anything, and an extra round
    # trip to populate a dropdown is a round trip the student waits through.
    # Authorized, because CLO text is course content.
    try:
        clo_options = [
            CLOOption(clo_id=c.clo_id, text=c.text, confirmed=bool(c.confirmed_by))
            for c in boundary.get_clos(offering_id, claims)
        ]
    except AuthorizationError as exc:
        # An empty selector is the honest render for a caller whose enrollment
        # cannot be confirmed — never another cohort's outcomes.
        log.warning("examprep status denied: %s", exc)
        clo_options = []

    stats = boundary.exam_pack_stats(offering_id)
    return ExamPrepStatus(
        clo_options=clo_options,
        agent_available=settings.agent_enabled,
        pack_loaded=True,
        questions=stats["questions"],
        clos=stats["clos"],
        low_confidence=stats["low_confidence"],
        earliest_year=stats["earliest"],
        latest_year=stats["latest"],
    )


@router.post("/plan")
async def plan(
    request: ExamPrepRequest,
    claims: StudentClaims = Depends(rate_limited),
) -> StreamingResponse:
    """Stream a study plan or a practice set.

    Authorization is layered exactly as it is on `/chat`: this route verifies the
    JWT and nothing more, and entitlement is re-derived at the
    `CourseIntelligence` boundary — per tool call, in the agent's case, which is
    stricter than per request and is why the boundary is where it is.
    """
    log.info(
        "examprep: user=%s offering=%s agent=%s req=%r",
        claims.sub, claims.offering_id, settings.agent_enabled, request.request[:80],
    )

    if settings.agent_enabled:
        # Imported here, not at module scope. With the flag off, the agent package
        # is never even imported — so a broken agent cannot take down the endpoint
        # that is supposed to be routing around it.
        from ..agents.runner import agent

        source = agent.stream(request, claims)
    else:
        from .plan import deterministic_plan

        source = deterministic_plan(request, claims)

    return StreamingResponse(
        _encode(source),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/study-plan")
async def study_plan(
    request: StudyPlanRequest,
    claims: StudentClaims = Depends(rate_limited),
) -> StudyPlan:
    """A revision plan sized by a marks budget (§7.4). JSON, not a stream.

    **Not streamed, deliberately.** This is arithmetic over data the service
    already has — a few SQL queries and a weighted split, no model call, no tool
    loop, milliseconds. Streaming it would mean inventing a frame type to carry
    structured items, and a `StudyPlan` is a value, not a narration. `/plan` next
    door streams prose because prose arrives a token at a time; this does not.

    **No concurrency slot.** Slots exist to bound how much of the provider's
    concurrency one student can hold open, and there is no provider here. Taking
    one would let a free local computation deny a student their practice
    questions, which is the opposite of what the limit is for. The per-minute
    rate limit still applies, through the same `rate_limited` dependency every
    other student route uses.

    Authorization is layered exactly as on `/plan`: this route verifies the JWT
    and nothing more, and enrollment is re-derived at the `CourseIntelligence`
    boundary on every read the planner performs — the CLO lookup and one search
    per outcome each call `_authorize` independently.
    """
    log.info(
        "study-plan: user=%s offering=%s budget=%d",
        claims.sub, claims.offering_id, request.marks_budget,
    )

    try:
        plan, report = await asyncio.to_thread(
            plan_for_offering,
            claims,
            marks_budget=request.marks_budget,
            snapshot=request.mastery,
        )
    except AuthorizationError as exc:
        # Fails closed, and says which failure it is. A student whose enrollment
        # cannot be confirmed gets 403 NOT_ENROLLED — never an empty plan, which
        # would read as "this course has nothing for you".
        log.warning("study-plan denied: %s", exc)
        raise HTTPException(status_code=403, detail=ErrorCode.NOT_ENROLLED.value) from exc

    # The report is diagnostic and stays out of the contract, but an empty plan
    # has four different causes and an operator reading the log needs to know
    # which one — no pack, no outcomes, nothing tagged, nothing with marks.
    log.info("study-plan for %s: %s", claims.offering_id, report.as_dict())
    return plan


@router.post("/practice/stream")
async def practice_stream(
    request: PracticeRequest,
    claims: StudentClaims = Depends(rate_limited),
) -> StreamingResponse:
    """Stream one generated practice question.

    **Not gated by `agent_enabled`, and that is deliberate.** The generator is a
    deterministic two-stage pipeline, not the agent — it makes one model call, no
    planning loop, and it runs on a default install. The instructor's
    `exam_prep_enabled` on the XBlock is what decides whether students see the
    tab at all; a second operator flag over a measured pipeline would be a
    control with nothing left to protect against.

    §9.0 permits this output to reach a student with no instructor approval
    because it is labelled, cited and measured. All three are enforced below this
    line rather than here: `quiz_generator` injects the label and the provenance,
    and `eval/feature_b_rubric.py` is the measurement.

    Authorization is layered exactly as on `/chat` and `/plan`: this route
    verifies the JWT and rate-limits, and entitlement is re-derived at the
    `CourseIntelligence` boundary on every read the generator performs — the
    source-question search, the CLO lookup and the lesson retrieval each call
    `_authorize` independently.
    """
    log.info(
        "practice: user=%s offering=%s clo=%s band=%s",
        claims.sub, claims.offering_id, request.clo_id, request.difficulty_band,
    )

    # Before the generator is touched. A slot taken after generation started
    # would be counting streams that are already consuming the thing it exists to
    # protect, and the third student would be refused only after their model call
    # was already in flight.
    #
    # Raises 429 with the same typed code the rate limiter uses, so the browser's
    # existing handler covers it: to a student, "too many at once" and "too many
    # per minute" are the same instruction — wait, then retry.
    token = rate_limiter.acquire_stream(claims.sub)

    from ..ai.quiz_generator import generator

    return StreamingResponse(
        _encode_holding_slot(
            generator.stream(
                claims, clo_id=request.clo_id, difficulty_band=request.difficulty_band
            ),
            claims.sub,
            token,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
