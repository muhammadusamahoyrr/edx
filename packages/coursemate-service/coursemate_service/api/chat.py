"""The student chat endpoint — design §3.4 rule 3 (v8).

**The browser calls this directly.** No LMS process is in the answer path: the
XBlock mints a token and returns in milliseconds, and this endpoint streams to the
browser over a same-origin path routed at the ingress.

That shape is the whole point. A streaming *proxy* through the XBlock would hold a
gunicorn worker for the entire generation — five to fifteen seconds — and the LMS
worker pool is exhausted by **occupancy**, not by computation. Two hundred students
streaming concurrently would be two hundred occupied workers, which is exactly the
incident the topology exists to prevent.

Phase 4 scope: plumbing only. The stream emits a scripted response so the
transport, auth and streaming behaviour can be verified independently of any
model. Phase 5 replaces `_scripted_answer` with a real LiteLLM call and nothing
else about this file changes — which is the point of building it this way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import ChatRequest, Citation, FrameType, StreamFrame
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..config import settings
from .deps import rate_limited

log = logging.getLogger(__name__)

router = APIRouter()


def _sse(frame: StreamFrame) -> str:
    """One Server-Sent Event.

    The blank line terminator is load-bearing: without it the browser buffers
    frames instead of dispatching them, and the stream appears to hang.
    """
    return f"data: {frame.model_dump_json(exclude_none=True)}\n\n"


async def _scripted_answer(request: ChatRequest, claims: StudentClaims) -> AsyncIterator[str]:
    """Stand-in for generation. Replaced by LiteLLM in Phase 5.

    Deliberately *slow* — a token every 150ms. A fast stub would prove nothing:
    the property under test is that a long-running generation occupies no LMS
    worker, and that is only observable when generation takes measurable time.
    """
    words = [
        "Phase", "4", "transport", "verified:", "this", "answer", "is",
        "streaming", "from", "the", "CourseMate", "service", "directly", "to",
        "your", "browser.", "No", "LMS", "worker", "is", "held", "open",
        "while", "this", "runs.",
    ]
    for word in words:
        await asyncio.sleep(0.15)
        yield _sse(StreamFrame(type=FrameType.TOKEN, text=word + " "))

    # Citation is mandatory in the real pipeline (§8.5): an answer that cannot
    # cite abstains. Emitted here so the frame type is exercised end to end.
    yield _sse(
        StreamFrame(
            type=FrameType.CITATION,
            citation=Citation(
                usage_key=claims.usage_key or "",
                display_name="(scripted — no retrieval yet)",
                url=None,
            ),
        )
    )
    yield _sse(StreamFrame(type=FrameType.DONE, provider="scripted"))


@router.post("/chat")
async def chat(
    request: ChatRequest,
    claims: StudentClaims = Depends(rate_limited),
) -> StreamingResponse:
    """Stream an answer to the browser.

    Authorization note, stated plainly because it is not finished: the JWT is
    verified here (signature, expiry, audience), but enrollment and role are
    **not yet re-checked against the platform**. §10.1 requires that on every
    call, and it lands with the boundary in Phase 6. Until then this endpoint
    trusts the token's scope, which is acceptable only because it serves no real
    course content yet.
    """
    log.info(
        "chat: user=%s offering=%s block=%s q=%r",
        claims.sub, claims.offering_id, claims.block_id, request.question[:80],
    )

    return StreamingResponse(
        _scripted_answer(request, claims),
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
