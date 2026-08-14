"""The agent loop — plan with tools, then synthesise.

    plan (tools, <= max_iterations)  ->  decide  ->  stream the answer

Two phases rather than one, because they want different things. Planning wants
tool schemas, no streaming, and a short output; synthesis wants no tools, full
streaming, and the grounding contract. Folding them together would mean either
streaming a message that turns out to be a tool call, or not streaming the answer
— and time-to-first-token is the number a student feels.

**Failure rules, and each one is a decision from the design pass:**

* *Empty is not failed.* `{}` mastery for a new student, or no question matching a
  narrow filter, is an answer. Retrying it burns the budget discovering nothing.
* *One re-plan per tool.* A tool that errors returns a typed message the model can
  act on. The **second** error from the same tool ends the loop — at that point it
  is broken, not misused, and further attempts are just latency.
* *No silent gap.* If any tool errored and the turn still answers, an `INCOMPLETE`
  frame is emitted and the model is told what it could not check. There is no path
  where a failed call reaches synthesis unremarked.
* *The confidence gate abstains the turn.* If `search_course_content` was gated,
  the turn abstains (decision 6). Conservative on purpose: it keeps the measured
  0-false-answer property true by construction, and the cost — more abstentions on
  multi-hop questions — is a number the agent eval set reports rather than a
  surprise.
* *Abandon, never resume.* There is no partial state to recover. The tools are
  read-only and idempotent, so restarting is cheap and correct; resuming would
  mean persisting mid-loop context whose consistency we cannot verify, and "a
  partial state that reports success" is this project's recurring bug.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import FrameType, StreamFrame
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import ExamPrepRequest

from ..ai.client import PRIMARY_DEPLOYMENT, NoModelConfigured, deployment_of, get_router
from ..config import settings
from . import tools as _tools  # noqa: F401  — registers the tools at import
from .prompts import AGENT_SYSTEM, SYNTHESIS_SYSTEM, render_tool_result
from .registry import UNPARSEABLE, ToolContext, ToolResult, registry

log = logging.getLogger(__name__)


class _Budget:
    """Wall clock across the whole turn.

    Distinct from `model_timeout_seconds`, which bounds ONE provider call. Six
    calls that each take 55 seconds is a five-and-a-half-minute request that never
    technically timed out — the student is gone long before it returns.
    """

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    @property
    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def spent(self) -> bool:
        return self.remaining <= 0


def _tool_calls(message) -> list:
    """Provider-shaped tool calls, normalised.

    LiteLLM already unifies the OpenAI/Anthropic shapes here, which is most of
    why the Router is worth its weight: the loop below is provider-agnostic
    because this is.
    """
    return list(getattr(message, "tool_calls", None) or [])


def _decode_args(raw):
    """Tool arguments arrive as a JSON string. Malformed ones are the model's
    error to fix, not ours to guess at, so this returns `UNPARSEABLE` and the
    registry turns it into a typed error the model can act on — rather than
    raising mid-loop.

    **`UNPARSEABLE` rather than `None`**, and the distinction is load-bearing.
    `None` means "no arguments", which several tools legitimately take. Returning
    `None` for a truncated JSON string meant `get_plan_context` executed normally on a
    garbled call, and nothing anywhere recorded that the model had malfunctioned.
    """
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return UNPARSEABLE
    # JSON `null` is how several providers spell "this tool takes no arguments".
    # Groq's llama-3.3-70b sends exactly `'null'` for every no-parameter tool,
    # measured three times out of three — so rejecting it cost a wasted round
    # trip on EVERY turn, which the profile caught only because a second provider
    # was tried. `None` here, not `{}`: a tool with required arguments then fails
    # validation naming the missing field, which is the right error.
    if parsed is None:
        return None
    # Valid JSON of the wrong shape — `"[1,2]"`, `"a string"`, `5`. The model
    # produced readable output that is not an argument object.
    return parsed if isinstance(parsed, dict) else UNPARSEABLE


class ExamPrepAgent:
    """Stateless across turns. Everything student-scoped lives in `ToolContext`."""

    async def stream(
        self, request: ExamPrepRequest, claims: StudentClaims
    ) -> AsyncIterator[StreamFrame]:
        """Yield frames for one exam-prep turn. Never raises — failures are frames.

        Same contract as `AnswerPipeline.stream`, deliberately: the transport
        layer (`api/examprep.py`) encodes frames and knows nothing else, so the
        agent and the chat pipeline are interchangeable from its point of view.
        """
        budget = _Budget(settings.agent_timeout_seconds)
        ctx = ToolContext(claims=claims, mastery=request.mastery)

        try:
            router = get_router()
        except NoModelConfigured:
            log.warning("no LLM provider configured; exam prep unavailable")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        messages: list[dict] = [
            {"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": request.request},
        ]

        results: list[ToolResult] = []
        #: Consecutive-failure count per tool. Keyed by tool name because the rule
        #: is per tool: one broken tool must not spend the retry allowance of the
        #: three that still work.
        failures: dict[str, int] = {}
        iterations = 0

        # --- phase 1: plan ------------------------------------------------
        while iterations < settings.agent_max_iterations:
            if budget.spent:
                log.warning(
                    "agent budget of %.1fs exhausted after %d iterations",
                    settings.agent_timeout_seconds, iterations,
                )
                break
            iterations += 1

            try:
                response = await asyncio.wait_for(
                    router.acompletion(
                        model=PRIMARY_DEPLOYMENT,
                        messages=messages,
                        tools=[{"type": "function", "function": s} for s in registry.schemas()],
                        max_tokens=settings.max_output_tokens,
                        **({"mock_response": settings.mock_response}
                           if settings.mock_response else {}),
                    ),
                    timeout=max(1.0, budget.remaining),
                )
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("agent planning call timed out")
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
                return
            except Exception:
                log.exception("agent planning call failed")
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
                return

            message = response.choices[0].message
            calls = _tool_calls(message)
            if not calls:
                # The model is ready to answer. Its draft is discarded: phase 2
                # regenerates under the grounding contract, and letting a
                # tools-phase message through would be an answer written under
                # the planning prompt, which has none of the citation rules.
                break

            messages.append(
                {"role": "assistant", "content": message.content or "",
                 "tool_calls": [c.model_dump() if hasattr(c, "model_dump") else c
                                for c in calls]}
            )

            aborted = False
            for call in calls:
                fn = call.function
                name = getattr(fn, "name", "") or ""
                args = _decode_args(getattr(fn, "arguments", None))

                # Tool handlers are blocking (SQLite, and an HTTP enrollment check
                # inside the boundary). Off the event loop, same rule as retrieval.
                result = await asyncio.to_thread(registry.invoke, name, args, ctx)
                results.append(result)

                if result.failed:
                    failures[name] = failures.get(name, 0) + 1
                    if failures[name] >= 2:
                        # Broken, not misused. Stop rather than spend the rest of
                        # the budget confirming it.
                        log.error("tool %s failed twice; ending the loop", name)
                        aborted = True
                else:
                    # Consecutive, not cumulative: a tool that recovers has not
                    # used up its allowance for the rest of the turn.
                    failures.pop(name, None)

                messages.append(render_tool_result(result))

            if aborted:
                yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
                return
        else:
            # `while ... else` runs when the loop was not broken out of — i.e. the
            # cap was reached with the model still asking for tools. Logged as a
            # defect: a ceiling that is routinely hit is a budget, and the number
            # belongs in the eval report rather than in a silent truncation.
            log.warning(
                "agent hit max_iterations=%d still requesting tools",
                settings.agent_max_iterations,
            )

        # --- decide -------------------------------------------------------
        if any(r.gated_out for r in results):
            # Decision 6: if a confidence-gated tool abstained, the turn abstains.
            log.info(
                "abstaining: %d gated tool result(s) in this turn",
                sum(1 for r in results if r.gated_out),
            )
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.ABSTAINED)
            return

        if not results:
            # No tool ran, so there is no evidence and nothing to ground an answer
            # in. Abstaining beats letting the model answer from its own knowledge
            # about the subject, which is the failure §8.5 exists to prevent.
            log.info("abstaining: the agent called no tools")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.ABSTAINED)
            return

        failed = _unrecovered(results)

        # --- phase 2: synthesise ------------------------------------------
        system = SYNTHESIS_SYSTEM
        if failed:
            system += (
                "\n\nThese tools failed during this turn and their information is "
                f"missing: {', '.join(failed)}. Say what you could not check.\n"
            )
        # The tool results are re-sent as their own message blocks (decision 8):
        # quoted data in the user role, never folded into the system prompt.
        synth: list[dict] = [{"role": "system", "content": system},
                             {"role": "user", "content": request.request}]
        synth += [render_tool_result(r) for r in results]

        provider_used: str | None = None
        deployment: str | None = None
        produced_any = False
        finish_reason: str | None = None

        try:
            stream = await asyncio.wait_for(
                router.acompletion(
                    model=PRIMARY_DEPLOYMENT,
                    messages=synth,
                    stream=True,
                    max_tokens=settings.max_output_tokens,
                    **({"mock_response": settings.mock_response}
                       if settings.mock_response else {}),
                ),
                timeout=settings.model_timeout_seconds,
            )
            async for part in stream:
                choice = (part.choices or [None])[0]
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if provider_used is None:
                    provider_used = getattr(part, "model", None) or "unknown"
                if deployment is None:
                    deployment = deployment_of(part)
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                if text:
                    produced_any = True
                    yield StreamFrame(type=FrameType.TOKEN, text=text)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("agent synthesis timed out")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return
        except Exception:
            log.exception("agent synthesis failed")
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        if not produced_any:
            yield StreamFrame(type=FrameType.ERROR, error_code=ErrorCode.UNAVAILABLE)
            return

        # --- citations ----------------------------------------------------
        # Only chunks that passed the gate ever reached `results`, so every
        # citation emitted here is one the gate approved. That is decision 6's
        # no-divergence rule, and it holds structurally rather than by check.
        for citation in _citations(results):
            yield StreamFrame(type=FrameType.CITATION, citation=citation)

        if failed:
            # Emitted AFTER the text and the citations, so the student reads the
            # answer and then the caveat rather than a warning about an answer
            # they have not seen.
            yield StreamFrame(type=FrameType.INCOMPLETE, text=", ".join(failed))

        if deployment is not None and deployment != PRIMARY_DEPLOYMENT:
            yield StreamFrame(type=FrameType.DEGRADED, provider=provider_used)

        yield StreamFrame(
            type=FrameType.DONE,
            provider=provider_used,
            truncated=finish_reason == "length",
        )


def _unrecovered(results: list[ToolResult]) -> list[str]:
    """Tools whose information is still missing when synthesis begins.

    **Last outcome per tool, not "ever failed".** A tool that errored and was then
    called successfully has supplied its information; marking the answer
    incomplete for it would be a warning the student cannot act on — the same
    reason `deployment_of` returning None is not treated as degradation. The
    caveat has to mean something, or students learn to ignore it.

    That distinction was not in the first version of this loop. The agent gold
    set found it: case a10 has the registry refuse a model-supplied identity
    field, the model re-plan without it, and the turn complete — and the turn was
    being marked incomplete over a refusal that had already been handled.
    """
    last: dict[str, bool] = {}
    for r in results:
        last[r.tool] = r.failed
    return sorted(tool for tool, did_fail in last.items() if did_fail)


def _citations(results: list[ToolResult]):
    """Every gate-approved source the turn drew on, de-duplicated in order.

    Built from tool RESULTS rather than from the model's output. The model cannot
    add a citation that no tool returned, and it cannot drop one either — which
    trades a little over-citation for the guarantee §8.5 actually wants: an answer
    that cannot cite does not ship uncited.
    """
    from coursemate_contracts.chat import Citation

    seen: set[str] = set()
    out: list[Citation] = []
    for r in results:
        if r.failed or not isinstance(r.data, dict):
            continue
        for chunk in r.data.get("chunks") or []:
            key = chunk.get("usage_key")
            if key and key not in seen:
                seen.add(key)
                out.append(
                    Citation(usage_key=key, display_name=chunk.get("display_name"))
                )
    return out


#: One instance per process. Holds no per-student state.
agent = ExamPrepAgent()
