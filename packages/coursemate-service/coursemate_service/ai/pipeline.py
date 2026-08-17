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
from coursemate_contracts.chat import ChatRequest, Citation, FrameType, StreamFrame
from coursemate_contracts.errors import ErrorCode

from .. import metrics, response_cache
from ..budget import estimate_tokens, ledger
from ..config import settings
from . import gate
from .client import PRIMARY_DEPLOYMENT, NoModelConfigured, deployment_of, get_router
from .context import ContextProvider, ContextResult
from .prompts import build_messages
from .query import is_outline_query, retrieval_query
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

    async def _fetch_outline(self, claims: StudentClaims) -> ContextResult | None:
        """The author's overview, or None when this provider cannot supply one.

        `ContextProvider` is an injection seam and several test doubles implement
        only `fetch`. Asking for the capability rather than assuming it keeps
        every existing double working and makes the fallback explicit: a provider
        without outline support routes outline questions down the ordinary path,
        which is exactly what happened before this branch existed.

        A failure here is not an error the student should see. The ordinary path
        is still available and still correct, so this degrades to it rather than
        ending the turn.
        """
        fetch_outline = getattr(self.context, "fetch_outline", None)
        if fetch_outline is None:
            return None
        try:
            return await fetch_outline(claims)
        except Exception:
            log.exception("outline fetch failed; falling back to retrieval")
            return None

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
        metrics.increment("chat_requests_total")

        # --- 0. outline questions are a different shape ---------------------
        # "List all the topics in this course" is a question about the course's
        # CONTENTS, and retrieval cannot answer it: `search` returns the
        # `rerank_top_k` passages most similar to the words typed, which is a
        # selection and therefore never exhaustive. Measured on the live course,
        # that is 3 of 55 chunks — and ten identical prompts produced ten
        # different lists, because the completeness the student asked for was
        # being improvised by the model from a 6% sample.
        #
        # Raising `top_k` does not fix it (a bigger selection is still a
        # selection, and `max_output_tokens` bounds the answer anyway), and
        # neither does `temperature=0` — measured, it narrows the spread and
        # still produced four different answers under a pinned provider and a
        # fixed seed. The fix is to stop asking a ranker a question about
        # structure.
        #
        # Placed before retrieval, and it returns without reaching the provider:
        # the answer is the author's own overview, so there is nothing for a
        # model to add and nothing for sampling to vary.
        if is_outline_query(request.question):
            outline = await self._fetch_outline(claims)
            if outline is not None and outline.index_missing:
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.PREPARING)
                return
            if outline is not None and outline.chunks:
                metrics.increment("outline_answers_total")
                for frame in _outline_frames(outline):
                    yield frame
                return
            # No author summaries — DemoX has none, and that is a real answer,
            # not a failure. Fall through to ordinary retrieval rather than
            # present three ranked passages as an overview. Nothing below claims
            # completeness, so no new wire signal is needed to stay honest; the
            # limitation is recorded in docs rather than invented as a frame.
            #
            # Counted, because the ratio of this to `outline_answers_total` is
            # what says whether courses in this deployment actually carry author
            # overviews — the measurement that decides whether the structural
            # fallback is worth building.
            metrics.increment("outline_fallthrough_total")

        query = retrieval_query(request.question, request.history, request.usage_key)
        try:
            context = await self.context.fetch(query, claims)
        except Exception:
            log.exception("context fetch failed")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        # --- 1b. first-turn cache read -------------------------------------
        # Placed AFTER retrieval and before everything else, which is the only
        # position that is both useful and safe:
        #
        # * after retrieval, because retrieval is what enforces enrollment,
        #   applies the group filter and writes the audit record. A hit that
        #   short-circuited those would be the §10.2 failure exactly — every
        #   filter correct, and the cache handing the answer to someone the
        #   filters would have refused. Retrieval costs ~3 ms locally; the call
        #   this skips costs ~55 s.
        # * before the gate, so an abstention can be served from cache too, and
        #   so a hit replays whatever the gate decided last time rather than
        #   re-deriving it.
        #
        # Replaying the gate's decision is sound because the decision is a
        # function of retrieval, and retrieval is a function of the query, the
        # caller's scope and the index version — all three of which are in the
        # key. A hit therefore cannot disagree with what the gate would have said.
        cache_key: str | None = None
        if response_cache.is_cacheable_request(request) and context.index_version:
            cache_key = response_cache.cache_key(request, claims, context.index_version)
            if (hit := response_cache.read(cache_key)) is not None:
                metrics.increment("cache_hits_total")
                # A replayed abstention is still an abstention. Counting it only
                # on the generated path would make `abstentions_total` mean
                # "abstentions we recomputed", so the rate would fall as the
                # cache warmed and read as the tutor answering more — the
                # opposite of what happened.
                if hit.get("kind") == "abstained":
                    metrics.increment("abstentions_total")
                async for frame in _replay(hit):
                    yield frame
                return
            metrics.increment("cache_misses_total")

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
            # ABSTAINED is cached; PREPARING is not. "Not covered in this course"
            # is a settled answer for this index version. "Still being prepared"
            # is a statement that the index is about to change, and caching a
            # claim whose whole content is "this is temporary" is the one entry
            # guaranteed to be wrong before its TTL expires.
            if code is ErrorCode.ABSTAINED:
                metrics.increment("abstentions_total")
            if cache_key is not None and code is ErrorCode.ABSTAINED:
                response_cache.write(
                    cache_key, {"kind": "abstained"}, context.chunks
                )
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
            metrics.increment("budget_refusals_total")
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
                    # **Variance reduction, NOT determinism.** No sampling
                    # parameter was sent at all until now, so the provider's own
                    # default applied and identical prompts produced materially
                    # different answers: measured on the live index, one
                    # byte-identical ordinary prompt gave **7 distinct answers in
                    # 10 runs**, and at temperature=0 the same prompt gave **1 in
                    # 5**.
                    #
                    # That is a large improvement and it is not a guarantee. The
                    # same experiment on a long, open-ended prompt still produced
                    # four different answers under temperature=0, seed=42, top_p=1
                    # AND a pinned upstream provider — the residual comes from
                    # batched GPU inference and cannot be closed from here. Short
                    # grounded answers converge; long enumerations do not.
                    #
                    # So this is worth having and must never be described as
                    # making the tutor reproducible. The outline path does not
                    # rely on it: it returns before this call and reaches no
                    # provider at all, which is what makes *it* deterministic.
                    temperature=0,
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
            metrics.increment("provider_failures_total")
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
            metrics.increment("provider_failures_total")
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
        cited = [context.chunks[idx].citation for idx in supporting_chunks(answer, chunk_texts)]
        for citation in cited:
            yield StreamFrame(type=FrameType.CITATION, citation=citation)

        # Sentences the retrieved material does not support. The frame is marked,
        # never rewritten: the student has already read the text, and silently
        # changing it under them is worse than telling them which part to doubt.
        unsupported: list[str] = []
        if settings.verify_claims:
            for claim in unsupported_sentences(
                answer, chunk_texts, settings.claim_support_threshold
            ):
                # Coverage and length, never the sentence. It was logged in full
                # (to 80 chars) until 2026-08-14, and an answer is derived from
                # the student's question — so the log held both halves of a
                # conversation §3.1 says stays with the platform. The number is
                # what tuning `claim_support_threshold` actually needs; the text
                # was never read for that and is on its way to the student's
                # screen regardless.
                log.info(
                    "unsupported claim: coverage=%.2f chars=%d",
                    claim.coverage, len(claim.sentence),
                )
                unsupported.append(claim.sentence)
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
            # Counted here, at the one place that already knows a fallback
            # answered. `provider_failures_total` cannot see this: the Router
            # swallows the failure when a fallback succeeds, so a primary
            # degrading on every request moved that counter by zero.
            metrics.increment("degraded_answers_total")
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

        # --- 6. cache the finished answer ------------------------------------
        # The completed payload, never streaming state: text, citations, marked
        # claims and the DONE metadata — everything needed to reproduce what this
        # caller just saw, and nothing about how it arrived in pieces.
        #
        # **A degraded answer is not cached.** It came from the fallback
        # deployment during an outage, and it is the worst answer the system
        # produces. Storing it would freeze the outage's quality into every
        # subsequent hit for the TTL, long after the primary recovered — and the
        # replayed DEGRADED frame would tell a student about an outage that had
        # ended. A truncated answer is cached: it is a complete record of a real
        # answer that hit the token cap, and the flag replays honestly.
        if cache_key is not None and deployment == PRIMARY_DEPLOYMENT:
            response_cache.write(
                cache_key,
                {
                    "kind": "answer",
                    "answer": answer,
                    "citations": [c.model_dump(mode="json") for c in cited],
                    "unsupported": unsupported,
                    "provider": provider_used,
                    "truncated": truncated,
                },
                context.chunks,
            )

        yield StreamFrame(type=FrameType.DONE, provider=provider_used, truncated=truncated)


#: What the outline answer calls itself.
#:
#: **It claims authorship, not completeness**, and the distinction is the whole
#: honesty of this path. The blocks are what the course author wrote as their
#: overview; whether that overview names every page is the author's decision and
#: not something this system can verify. "Here is every topic in this course"
#: would be a claim about the course; this is a claim about the source, which is
#: the only one the data supports.
_OUTLINE_HEADING = "This course's author-provided overview covers:"

#: Said plainly at the end rather than buried, because the failure this whole
#: change exists to fix was a partial answer that read as a complete one.
_OUTLINE_CAVEAT = (
    "This is the overview written by the course author. It may not name every "
    "page in the course."
)


def _outline_frames(result: ContextResult) -> list[StreamFrame]:
    """The outline answer, as frames. Pure, and deliberately so.

    No provider call, no sampling, no ranking — the same input produces the same
    bytes every time, which is the property the ordinary path cannot offer and
    the reason this path exists.

    Reuses TOKEN and CITATION rather than adding a frame type: the browser
    already renders both, so this needs no contract version bump and no platform
    change to ship. One TOKEN carries the whole answer, matching `_replay`'s
    reasoning — re-simulating a typewriter would mean inventing timings that
    never happened.

    Citations are emitted per block and are never narrowed by
    `supporting_chunks`. That filter exists to turn "we searched this" into "the
    answer used this"; here the answer *is* the blocks, so every one of them
    contributed by construction and dropping any would under-cite.
    """
    body = [_OUTLINE_HEADING, ""]
    for chunk in result.chunks:
        body.append(f"## {chunk.citation.display_name}")
        body.append(chunk.text.strip())
        body.append("")
    body.append(_OUTLINE_CAVEAT)

    frames = [StreamFrame(type=FrameType.TOKEN, text="\n".join(body))]
    frames += [
        StreamFrame(type=FrameType.CITATION, citation=chunk.citation)
        for chunk in result.chunks
    ]
    frames.append(StreamFrame(type=FrameType.DONE, provider=None, truncated=False))
    return frames


async def _replay(payload: dict) -> AsyncIterator[StreamFrame]:
    """Turn a cached payload back into the frames the UI already handles.

    No new frame type and no "this was cached" marker. The student asked a
    question and got the answer to it; where it came from is an implementation
    detail, and a badge saying otherwise would invite them to distrust an answer
    that is byte-identical to the one they would have waited 55 seconds for.

    The whole answer arrives as ONE token frame rather than re-simulated
    streaming. Faking a typewriter would mean inventing timings that never
    happened, and the UI appends token text either way.
    """
    if payload.get("kind") == "abstained":
        yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.ABSTAINED)
        return

    if answer := payload.get("answer"):
        yield StreamFrame(type=FrameType.TOKEN, text=answer)
    for citation in payload.get("citations") or []:
        yield StreamFrame(type=FrameType.CITATION, citation=Citation(**citation))
    for sentence in payload.get("unsupported") or []:
        yield StreamFrame(type=FrameType.UNSUPPORTED_CLAIM, text=sentence)
    yield StreamFrame(
        type=FrameType.DONE,
        provider=payload.get("provider"),
        truncated=bool(payload.get("truncated")),
    )


#: One instance per process. Stateless — conversation state stays with the
#: platform (§3.1), so this holds no per-student data and is safe to share.
pipeline = AnswerPipeline()
