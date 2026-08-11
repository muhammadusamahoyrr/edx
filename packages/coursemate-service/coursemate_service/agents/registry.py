"""The tool registry — where identity is injected and never accepted.

Every tool call passes through `invoke()`, and it does four things in this order:

    1. reject model-supplied identity     (decision 2 — reject, never override)
    2. validate against a strict schema   (decision 3 — before execution)
    3. execute with injected context      (claims come from the verified token)
    4. classify the outcome               (ok / gated / error — three, not two)

Step 1 before step 2 is deliberate. `extra="forbid"` would already reject an
`offering_id` argument, but as a generic "unexpected field" — indistinguishable
in the logs from a model typo. A prompt-injection attempt and a hallucinated
argument deserve different log lines, and only one of them is worth waking up for.

**Three outcomes, not two.** `ERROR` means the lookup broke. `GATED` means the
lookup worked and the confidence gate said the course does not cover this — an
*answer*, and the one this project tunes toward (§8.5). `OK` with an empty payload
is also an answer: a student with no attempt history has `{}` mastery, and a runner
that retried that until the iteration cap would burn the whole budget discovering
nothing was wrong. Collapsing these three into a boolean is how "a failed tool call
reached synthesis silently" happens, which decision 7 forbids.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.mastery import MasterySnapshot
from pydantic import BaseModel, ValidationError

from .schemas import IDENTITY_FIELDS

log = logging.getLogger(__name__)


class _Unparseable:
    """Sentinel: the model sent arguments we could not read.

    Distinct from `None`, which means it sent none — and a tool that legitimately
    takes no arguments accepts that. Collapsing the two let a truncated JSON
    string decode to `None`, fall through `args = raw_args or {}`, and *succeed*
    on a no-argument tool: a garbled call producing a clean result with nothing
    recording that the model had malfunctioned.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unparseable tool arguments>"


#: Passed by the runner when `json.loads` fails on the arguments string.
UNPARSEABLE = _Unparseable()


class IdentityFromModel(Exception):
    """The model tried to supply identity. Never overridden, always refused."""


class ToolStatus(StrEnum):
    OK = "ok"
    #: The confidence gate declined. Correct behaviour, not a fault.
    GATED = "gated"
    ERROR = "error"


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs that the model is not allowed to influence.

    `claims` comes from the verified JWT. `mastery` is carried by the browser from
    platform-owned `Scope.user_state` (§3.1) — attacker-controlled in the same
    sense the chat history is, which is why it may shape a student's own study
    plan and may never widen retrieval scope. Scope comes from `claims` alone.
    """

    claims: StudentClaims
    mastery: MasterySnapshot | None = None


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: ToolStatus
    #: JSON-serialisable. Rendered into its own message block by the runner,
    #: never concatenated into the system prompt (decision 8).
    data: Any = None
    #: One sentence, model-facing. On ERROR it says what to do next, because a
    #: message the model cannot act on just burns an iteration.
    message: str = ""
    #: True when the **confidence gate** (`ai.gate`) decided this result, as
    #: distinct from a tool that merely found nothing or has no data loaded.
    #:
    #: Decision 6's abstain rule keys on this flag rather than on GATED, and the
    #: difference is not pedantry. The confidence gate is the thing whose
    #: measured 0-false-answer property must be preserved, so a turn abstains
    #: when it fires. "No past papers were loaded for this course" is also a
    #: legitimate empty answer, but abstaining the whole turn on it would refuse
    #: to build a study plan from course content and CLOs — which the agent can
    #: do perfectly well, and honestly.
    gate_applied: bool = False

    @property
    def failed(self) -> bool:
        return self.status is ToolStatus.ERROR

    @property
    def gated_out(self) -> bool:
        """The confidence gate declined this call. Forces the turn to abstain."""
        return self.gate_applied and self.status is ToolStatus.GATED


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], ToolResult]

    def json_schema(self) -> dict:
        """The schema handed to the model, in **OpenAI function shape**.

        The key is `parameters`, and that is not cosmetic. The runner wraps this
        in `{"type": "function", "function": ...}`, which is the OpenAI envelope,
        and LiteLLM translates from there to whatever the provider wants.

        **This said `input_schema` — the Anthropic key — until 2026-08-11, and the
        consequence was total.** Inside an OpenAI envelope that key is unknown, so
        every provider saw tools declared with *no parameters whatsoever*. Groq
        validates server-side and rejected every call with "additionalProperties
        'clo_id', 'exam_type', … not allowed", naming the very fields the schema
        was supposed to declare. Ollama does not validate, so locally the model
        simply guessed argument names with nothing to guide it — which is exactly
        the invented `learning_outcome` / `earliest_year` / `minimum_marks` and
        the list-valued `clo_id` that earlier profiling blamed on the model.

        A schema nothing enforces is the same defect this project keeps finding:
        the strict-validation story in this file's docstring was true of
        `invoke()` and false of everything upstream of it.
        """
        return {
            "name": self.name,
            "description": self.description,
            # Pydantic emits `additionalProperties: false` from `extra="forbid"`,
            # which providers with strict function calling enforce on their side —
            # so a well-behaved provider never sends an invented argument at all,
            # and `invoke()` catches the rest.
            "parameters": self.args_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            # A duplicate name silently shadowing an earlier tool is the same
            # class of bug as two LiteLLM deployments sharing a `model_name`:
            # everything works, and half the traffic goes somewhere unintended.
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        return [self._tools[n].json_schema() for n in self.names()]

    def invoke(self, name: str, raw_args: dict | None, ctx: ToolContext) -> ToolResult:
        """Run one tool call. Never raises — every failure becomes a `ToolResult`.

        Raising here would end the loop on the model's first malformed argument,
        which is both common and recoverable: the model can be told what was wrong
        and re-plan. What must never happen is the opposite — a failure that
        *doesn't* surface — so the ERROR status is what the runner counts, and
        decision 7 makes an uncounted failure impossible to reach synthesis.
        """
        tool = self._tools.get(name)
        if tool is None:
            log.warning("agent requested unknown tool %r", name)
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                message=f"No such tool. Available: {', '.join(self.names())}.",
            )

        if raw_args is UNPARSEABLE:
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                message="Your arguments were not valid JSON. Send them again as a "
                        "JSON object.",
            )

        args = {} if raw_args is None else raw_args
        if not isinstance(args, dict):
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                message="Arguments must be a JSON object.",
            )

        # --- 1. identity is injected, never accepted (decision 2) ----------
        supplied = IDENTITY_FIELDS & set(args)
        if supplied:
            # Logged at WARNING with the field names but NOT their values: the
            # values are attacker-controlled and would be reflected into the
            # operator's log. The field name is what an operator needs.
            log.warning(
                "agent supplied identity fields %s to %s; refusing (user=%s offering=%s)",
                sorted(supplied), name, ctx.claims.sub, ctx.claims.offering_id,
            )
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                message=(
                    f"Refused: {', '.join(sorted(supplied))} cannot be supplied. "
                    "Identity and course scope come from the student's session. "
                    "Call the tool again without those fields."
                ),
            )

        # --- 2. strict validation, before anything executes (decision 3) ---
        try:
            parsed = tool.args_model(**args)
        except ValidationError as exc:
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                # Pydantic's message names the field and the constraint, which is
                # exactly what the model needs to fix the call on the next turn.
                message=f"Invalid arguments: {exc.error_count()} problem(s). "
                        + "; ".join(
                            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                            for e in exc.errors()[:4]
                        ),
            )

        # --- 3. execute -----------------------------------------------------
        try:
            return tool.handler(parsed, ctx)
        except Exception as exc:  # noqa: BLE001
            # The handler's own exception text is deliberately not forwarded to
            # the model: it can carry SQL, paths and offering ids from other
            # tenants. The operator gets the traceback, the model gets a fact.
            log.exception("tool %s raised %s", name, type(exc).__name__)
            return ToolResult(
                tool=name,
                status=ToolStatus.ERROR,
                message="The tool failed. Do not retry it more than once.",
            )


#: Populated by `tools.py` at import. One registry per process; it holds no
#: per-student state — everything student-scoped arrives in `ToolContext`.
registry = ToolRegistry()
