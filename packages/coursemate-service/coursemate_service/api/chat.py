"""The student chat endpoint — design §3.4 rule 3 (v8).

**The browser calls this directly.** No LMS process is in the answer path: the
XBlock mints a token and returns in milliseconds, and this endpoint streams to the
browser over a same-origin path routed at the ingress.

That shape is the whole point. A streaming *proxy* through the XBlock would hold a
gunicorn worker for the entire generation — five to fifteen seconds — and the LMS
worker pool is exhausted by **occupancy**, not by computation. Two hundred students
streaming concurrently would be two hundred occupied workers, which is exactly the
incident the topology exists to prevent.

**This file is transport, and nothing else.** Authentication, rate limiting and
SSE encoding happen here; what to say is decided entirely by `ai/pipeline.py`.
That split has now been paid off twice — Phase 5 replaced a scripted answer with
a real LiteLLM call, and Phase 6 added retrieval, the confidence gate, reranking
and claim verification, and neither touched this file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import ChatRequest, StreamFrame
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..ai.pipeline import pipeline
from .deps import rate_limited

log = logging.getLogger(__name__)

router = APIRouter()


def _sse(frame: StreamFrame) -> str:
    """One Server-Sent Event.

    The blank line terminator is load-bearing: without it the browser buffers
    frames instead of dispatching them, and the stream appears to hang.
    """
    return f"data: {frame.model_dump_json(exclude_none=True)}\n\n"


async def _encode(request: ChatRequest, claims: StudentClaims) -> AsyncIterator[str]:
    """Adapt pipeline frames to the SSE wire format.

    This is the entire generation-side responsibility of the transport layer: the
    pipeline decides *what* to say, this decides *how it is framed on the wire*.
    Keeping the split here is what lets retrieval, reranking and query rewriting
    land in Phase 6 without the API changing shape.
    """
    async for frame in pipeline.stream(request, claims):
        yield _sse(frame)


@router.post("/chat")
async def chat(
    request: ChatRequest,
    claims: StudentClaims = Depends(rate_limited),
) -> StreamingResponse:
    """Stream an answer to the browser.

    Authorization happens in two places, and this route is only the first of
    them. `rate_limited` verifies the JWT's signature, expiry and audience, and
    rejects a service credential presented on the student path — which
    establishes *who is asking* and nothing more.

    Entitlement is re-derived deeper in, at the CourseIntelligence boundary
    (`boundary/impl.py`), which asks the platform whether this user is still
    enrolled before any content is a retrieval candidate. §10.1 requires that on
    every call, because a signed token outlives the enrollment it was minted
    under: unenroll a student and their unexpired token still verifies here.

    It is deliberately not done in this route. Putting it at the boundary is
    what makes it unskippable — every path to course content goes through that
    one method, so a future endpoint cannot forget it.
    """
    log.info(
        "chat: user=%s offering=%s block=%s q=%r",
        claims.sub, claims.offering_id, claims.block_id, request.question[:80],
    )

    return StreamingResponse(
        _encode(request, claims),
        media_type="text/event-stream",
        headers={
            # Defeat proxy buffering. Without these an intermediary may hold the
            # whole response and deliver it at once, which looks identical to a
            # hung stream from the browser's side.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/whoami")
async def whoami(claims: StudentClaims = Depends(rate_limited)) -> dict:
    """Cheap, non-streaming proof that a minted token verifies end to end.

    Exists so token verification can be tested without waiting out a stream —
    when the stream misbehaves, this isolates auth from transport.
    """
    return json.loads(claims.model_dump_json())
