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

from ..budget import estimate_tokens, ledger
from ..config import settings
from . import gate
from .client import PRIMARY_DEPLOYMENT, NoModelConfigured, deployment_of, get_router
from .context import ContextProvider
from .prompts import build_messages
from .query import retrieval_query
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
        # What we SEARCH for is not what the student typed. On a follow-up the
        # bare question carries no topic — "why?" is unsearchable — so the query
        # is built from the conversation as well. `build_messages` below still
        # gets `request.question`: the model must see what was actually asked,
        # and only the retriever needs the reconstruction.
        query = retrieval_query(request.question, request.history, request.usage_key)
        try:
            context = await self.context.fetch(query, claims)
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

        # --- 2b. spend ceiling, also BEFORE the provider call ---------------
        # Same philosophy as the gate above and placed deliberately after it.
        # Retrieval and the gate are local and free, so running them first costs
        # nothing and keeps abstention honest: a student who is out of budget and
        # asks something this course does not cover still gets ABSTAINED, which
        # is the true answer, rather than being told about a limit that was never
        # going to be charged.
        #
        # Identity comes from the verified claims and nowhere else. `ChatRequest`
        # has no field that could name a different student, so a request cannot
        # ask to be billed to someone else — it can only be refused (invariant 9).
        if ledger.would_exceed(claims.offering_id, claims.sub):
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.BUDGET_EXCEEDED)
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
        #: Total tokens as the PROVIDER reported them, or None if it never did.
        #: The distinction is load-bearing — see `_bill`.
        reported_tokens: int | None = None

        def _bill() -> None:
            """Charge the day's ledger for what this turn actually cost.

            Two sources, in order of trust:

            1. What the provider reported. That is the number being billed, so it
               is the number to count — including on a turn that produced no text
               but still consumed a prompt.
            2. Failing that, an estimate over the text that really moved. Only
               when tokens demonstrably streamed.

            **A turn with no provider report and no output is not charged.** An
            abstention never reaches here, and a provider that failed before
            emitting anything bills nobody — counting either would make the
            ledger a request counter wearing a token's name, and would let a
            broken provider exhaust a student's day for free.
            """
            if reported_tokens:
                ledger.record(claims.offering_id, claims.sub, reported_tokens)
            elif produced_any:
                ledger.record(
                    claims.offering_id, claims.sub, estimate_tokens(messages, answer_parts)
                )

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
                # BEFORE the `choices` guard below, not after. A provider that
                # reports usage sends it on a final chunk with an EMPTY choices
                # list, so reading it further down would skip the only chunk that
                # carries the real cost and silently fall back to the estimate.
                if (usage := getattr(part, "usage", None)) is not None:
                    total = getattr(usage, "total_tokens", None)
                    if isinstance(total, int) and total > 0:
                        reported_tokens = total

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
            # A timeout after partial output still spent those tokens. The
            # student sees UNAVAILABLE — unchanged — but the bill is real and
            # not charging it would make a timing-out provider the cheapest way
            # to use the tutor.
            _bill()
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return
        except Exception as exc:
            # Covers the case the design calls out explicitly: both hosted
            # providers down means the tutor is unavailable and says so, rather
            # than fabricating an answer (§8.4).
            log.exception("generation failed: %s", type(exc).__name__)
            _bill()
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        if not produced_any:
            # `_bill` charges here only if the provider itself reported usage —
            # a prompt was submitted and metered even though nothing came back.
            _bill()
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        _bill()

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
