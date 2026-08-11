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

import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import StreamFrame
from coursemate_contracts.examprep import (
    CLOOption,
    ExamPrepRequest,
    ExamPrepStatus,
    PracticeRequest,
)
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..boundary.impl import AuthorizationError, boundary
from ..config import settings
from .deps import rate_limited

log = logging.getLogger(__name__)

router = APIRouter()


def _sse(frame: StreamFrame) -> str:
    return f"data: {frame.model_dump_json(exclude_none=True)}\n\n"


async def _encode(source: AsyncIterator[StreamFrame]) -> AsyncIterator[str]:
    async for frame in source:
        yield _sse(frame)


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

    from ..ai.quiz_generator import generator

    return StreamingResponse(
        _encode(generator.stream(
            claims, clo_id=request.clo_id, difficulty_band=request.difficulty_band
        )),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
