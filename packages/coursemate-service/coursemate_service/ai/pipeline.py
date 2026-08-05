"""The answer pipeline — all AI orchestration lives here, never in a route.

    retrieve context → gate on confidence → build messages → stream → cite

The route's entire job is authentication, rate limiting and SSE encoding. That
separation is what lets Phase 6 add retrieval, reranking and query rewriting
without touching the API surface: they are steps in *this* function.

The pipeline yields `StreamFrame` objects, not bytes. Encoding is the transport's
concern, so the same pipeline can later feed a WebSocket, a batch evaluation run,
or the offline harness (§11) without change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import ChatRequest, FrameType, StreamFrame
from coursemate_contracts.errors import ErrorCode

from ..config import settings
from .client import NoModelConfigured, get_router
from .context import ContextProvider
from .prompts import build_messages

log = logging.getLogger(__name__)


class AnswerPipeline:
    def __init__(self, context_provider: ContextProvider | None = None) -> None:
        # Injected, so retrieval can be replaced (or stubbed in tests) without
        # editing this class. Phase 6 swapped NullContextProvider for the real
        # one here and nothing else in the pipeline changed.
        if context_provider is None:
            from .retrieval import CourseContextProvider
            context_provider = CourseContextProvider()
        self.context = context_provider

    async def stream(
        self, request: ChatRequest, claims: StudentClaims
    ) -> AsyncIterator[StreamFrame]:
        """Yield frames for one question. Never raises — failures become frames.

        A generator that raises mid-stream leaves the browser with a truncated
        answer and no explanation. Every failure path here ends in an ERROR frame
        with a typed code the UI already knows how to render (§5.1).
        """
        # --- 1. retrieve --------------------------------------------------
        try:
            context = await self.context.fetch(request.question, claims)
        except Exception:
            log.exception("context fetch failed")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        # --- 2. confidence gate, BEFORE generating a token (§8.5) ----------
        # Free, and it is why abstention costs no latency: nothing streams that
        # already failed the retrieval bar.
        if settings.require_grounding:
            if context.index_missing:
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.PREPARING)
                return
            if context.is_empty or context.top_score < settings.confidence_threshold:
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.ABSTAINED)
                return

        # --- 3. build messages --------------------------------------------
        messages = build_messages(
            question=request.question,
            history=request.history,
            context=context,
            mode=request.mode,
            require_grounding=settings.require_grounding,
        )

        # --- 4. generate ---------------------------------------------------
        try:
            router = get_router()
        except NoModelConfigured:
            log.warning("no LLM provider configured; tutor unavailable")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        provider_used: str | None = None
        produced_any = False
        finish_reason: str | None = None

        try:
            response = await asyncio.wait_for(
                router.acompletion(
                    model="strong",
                    messages=messages,
                    stream=True,
                    max_tokens=settings.max_output_tokens,
                    **({"mock_response": settings.mock_response} if settings.mock_response else {}),
                ),
                timeout=settings.model_timeout_seconds,
            )

            async for part in response:
                choice = (part.choices or [None])[0]
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if provider_used is None:
                    provider_used = getattr(part, "model", None) or "unknown"
                # Carried from whichever chunk sets it — providers put it on the
                # last one, but not all of them agree on which.
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                if text:
                    produced_any = True
                    yield StreamFrame(type=FrameType.TOKEN, text=text)

        except asyncio.TimeoutError:
            log.warning("generation timed out after %ss", settings.model_timeout_seconds)
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return
        except Exception as exc:  # noqa: BLE001
            # Covers the case the design calls out explicitly: both hosted
            # providers down means the tutor is unavailable and says so, rather
            # than fabricating an answer (§8.4).
            log.exception("generation failed: %s", type(exc).__name__)
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        if not produced_any:
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        # --- 5. attribution -------------------------------------------------
        # Citations are emitted after the text so the UI can attach them to the
        # answer it already rendered. Mandatory once retrieval exists (§8.5): an
        # answer that cannot cite must abstain rather than ship uncited.
        for chunk in context.chunks:
            yield StreamFrame(type=FrameType.CITATION, citation=chunk.citation)

        # Surfaced so an outage reads as "answered by a fallback" rather than as
        # unexplained quality loss (§8.4 rule 3).
        if provider_used and settings.strong_model and provider_used not in settings.strong_model:
            yield StreamFrame(type=FrameType.DEGRADED, provider=provider_used)

        # `length` means the model was cut off at max_output_tokens, not that it
        # finished. Without this the student sees an answer that just stops, and
        # reads it as the tutor not knowing the rest.
        truncated = finish_reason == "length"
        if truncated:
            log.warning(
                "answer truncated at max_output_tokens=%s; raise it or tighten the prompt",
                settings.max_output_tokens,
            )

        yield StreamFrame(type=FrameType.DONE, provider=provider_used, truncated=truncated)


#: One instance per process. Stateless — conversation state stays with the
#: platform (§3.1), so this holds no per-student data and is safe to share.
pipeline = AnswerPipeline()
