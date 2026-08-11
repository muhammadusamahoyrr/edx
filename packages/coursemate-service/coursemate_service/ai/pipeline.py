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
from . import gate
from .client import PRIMARY_DEPLOYMENT, NoModelConfigured, deployment_of, get_router
from .context import ContextProvider
from .prompts import build_messages
from .verify import supporting_chunks, unsupported_sentences

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
        #
        # The decision moved to `gate.evaluate` when the exam-prep agent needed to
        # run the same gate per tool call. Behaviour is identical — the threshold,
        # the comparison and the check order are unchanged, and the tests that
        # pinned this path pass untouched. What changed is that there is now one
        # implementation instead of the two a copy-paste would have produced.
        outcome = gate.evaluate(context)
        if (code := gate.ERROR_CODE[outcome]) is not None:
            yield StreamFrame(type=FrameType.ERROR, error_code=code)
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
        #: Which of the Router's OWN deployments answered — "strong", "fallback"
        #: or "cheap". Distinct from `provider_used`, which is the model string
        #: the vendor echoed back and is for display only.
        deployment: str | None = None
        produced_any = False
        finish_reason: str | None = None
        # Accumulated for verification after the stream. Kept in memory for the
        # length of one answer and never stored — §3.1 keeps conversation text
        # with the platform, and this module holds no per-student state.
        answer_parts: list[str] = []

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
                if deployment is None:
                    deployment = deployment_of(part)
                # Carried from whichever chunk sets it — providers put it on the
                # last one, but not all of them agree on which.
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                if text:
                    produced_any = True
                    answer_parts.append(text)
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

        # --- 5. attribution and verification ---------------------------------
        # Both run after the stream, on the assembled answer. Streaming is
        # already done, so this adds nothing to time-to-first-token; it costs a
        # few milliseconds of set arithmetic before the citations appear.
        answer = "".join(answer_parts)
        chunk_texts = [c.text for c in context.chunks]

        # Citations are emitted after the text so the UI can attach them to the
        # answer it already rendered. Mandatory once retrieval exists (§8.5): an
        # answer that cannot cite must abstain rather than ship uncited.
        #
        # Narrowed to the chunks the answer actually drew on. Emitting all of
        # them made a citation mean "we searched this" rather than "the answer
        # used this" — three authoritative links under a sentence none of them
        # support. `supporting_chunks` returns everything when nothing overlaps,
        # so the mandatory-citation promise still holds in the worst case.
        for idx in supporting_chunks(answer, chunk_texts):
            yield StreamFrame(type=FrameType.CITATION, citation=context.chunks[idx].citation)

        # Sentences the retrieved material does not support. The frame is marked,
        # never rewritten: the student has already read the text, and silently
        # changing it under them is worse than telling them which part to doubt.
        if settings.verify_claims:
            for claim in unsupported_sentences(
                answer, chunk_texts, settings.claim_support_threshold
            ):
                log.info(
                    "unsupported claim (coverage %.2f): %.80s", claim.coverage, claim.sentence
                )
                yield StreamFrame(type=FrameType.UNSUPPORTED_CLAIM, text=claim.sentence)

        # Surfaced so an outage reads as "answered by a fallback" rather than as
        # unexplained quality loss (§8.4 rule 3).
        #
        # Decided on the Router's deployment name, never on the model string.
        # The old test — `provider_used not in settings.strong_model` — was a
        # substring match against configuration, and providers return versioned
        # ids, so a healthy `claude-opus-5-20260514` answering a configured
        # `anthropic/claude-opus-5` would have marked EVERY answer degraded.
        #
        # `None` means the deployment could not be identified, and it is not
        # treated as degradation: a warning the student cannot act on, raised
        # because we failed to look something up, is worse than none.
        if deployment is not None and deployment != PRIMARY_DEPLOYMENT:
            log.warning(
                "answered by the %s deployment (%s), not the primary",
                deployment, provider_used,
            )
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
