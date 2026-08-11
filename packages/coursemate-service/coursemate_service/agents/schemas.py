"""Tool argument schemas — strict, validated before anything executes.

Three properties, and each one closes a specific hole:

1. **`extra="forbid"`.** A model that invents an argument gets a typed error, not
   a silently ignored field. The dangerous version of that is an invented
   `student_id`: pydantic's default would drop it and the call would look clean,
   so an injection attempt would leave no trace anywhere.

2. **No identity fields exist on any schema.** `offering_id`, `student_id` and
   `tenant` are injected by the registry from the verified request context and
   are not declarable by the model. Combined with (1), a model that supplies one
   gets `ValidationError` — which is the point of decision 2: *reject*
   model-supplied identity rather than override it, because overriding hides the
   attempt.

3. **Bounded numbers.** `limit` is capped. An agent asking for 10,000 questions is
   a cost and latency event, and "the model asked for it" is not a reason.

This mirrors what `coursemate_contracts` already does at the HTTP boundary. The
model is simply another untrusted caller — the least trusted one in the system,
since its input is partly written by whoever wrote the course content it read.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

#: One learning-outcome id.
#:
#: Declared as an alias so the length cap sits on the STRING rather than on the
#: `str | list[str]` union. Writing `Field(max_length=64)` on the union made
#: pydantic emit `maxLength` against the `anyOf` wrapper itself — meaningless
#: JSON Schema, and Groq's server-side tool-call validator rejected the whole
#: request with "parameters did not match schema". Invisible locally, because
#: Ollama does not validate tool arguments against the schema at all.
CloId = Annotated[str, Field(max_length=64)]


class _ToolArgs(BaseModel):
    """Base for every tool's arguments.

    `model_config` is set once here so a new tool cannot forget it. Forgetting it
    is a silent failure — the tool works, extra fields are dropped, and nothing
    reports that the model tried to pass identity.
    """

    model_config = ConfigDict(extra="forbid")


class SearchCourseContentArgs(_ToolArgs):
    """Retrieval over published course content. Gated per call (decision 6)."""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


class SearchPastQuestionsArgs(_ToolArgs):
    """The structured filter §7.6 exists for — not a semantic search."""

    query: str | None = Field(default=None, max_length=500)
    #: One outcome, or several at once.
    #:
    #: **The list form was added because the merge created the need for it.** Once
    #: `get_plan_context` hands the planner every outcome in one call, the natural
    #: next move is to search them together — and the model did exactly that,
    #: sending `["CLO-1", "CLO-2"]` to a field typed `str`. The schema refused it
    #: twice and the turn died UNAVAILABLE.
    #:
    #: Widening the type is the right fix rather than a better error message: one
    #: call for N outcomes is one round trip instead of N, which is the same
    #: latency argument that motivated the merge. `IN (...)` costs the store
    #: nothing.
    clo_id: CloId | list[CloId] | None = None
    exam_type: Literal["mid", "final", "quiz", "assignment"] | None = None
    #: "Only the last 3 years." Floored at 1900 so a typo'd 20223 is a typed
    #: error rather than a filter that silently matches nothing.
    #:
    #: Named `earliest_year` rather than the internal `year_from`, and the honest
    #: history is worth keeping because the first diagnosis was wrong.
    #:
    #: The model was observed inventing `earliest_year` and `minimum_marks`, which
    #: looked like "the terse names are unguessable" — so they were renamed. The
    #: real cause was found afterwards: `Tool.json_schema()` was emitting the
    #: schema under Anthropic's `input_schema` key inside an OpenAI envelope, so
    #: **no provider ever received any parameter schema at all** and the model was
    #: guessing from the description alone. With the key fixed, either naming
    #: works.
    #:
    #: The rename is kept anyway — a model-facing argument name should read like
    #: one, and it costs nothing — but it fixed a symptom, not the bug. The
    #: internal names in the store and the boundary are unchanged; `tools.py`
    #: maps between them in one place.
    earliest_year: int | None = Field(default=None, ge=1900, le=2100)
    minimum_marks: int | None = Field(default=None, ge=0, le=1000)
    limit: int = Field(default=10, ge=1, le=25)


class GetPlanContextArgs(_ToolArgs):
    """No arguments. Everything a revision plan needs, in one call.

    **This replaced two tools — `get_clos` and `get_mastery` — and the merge was
    a latency fix, measured.** Profiling showed the planner spending 145 s of a
    222 s turn on six model round trips, one per tool call, because
    `qwen2.5:7b` emits exactly one tool call per turn no matter how it is
    prompted (three prompt rewrites were tried and reverted; see
    `docs/prompts.md`). Each round trip costs ~26 s while the tool underneath
    costs ~1 ms, so the only way to spend less time is to need fewer calls.

    Merging is the version of that which works on *every* model rather than only
    on ones that batch: the two facts a plan always needs — what the outcomes are
    and how the student is doing on them — now arrive together, and neither is
    worth a separate round trip because a planner needs both every time.

    It also carries `past_papers_available`, which is not a merge but a
    consequence of the same reasoning: knowing there is nothing to search saves a
    whole wasted round.
    """


#: Every field name that identity could hide behind. The registry rejects a call
#: whose raw arguments contain any of these BEFORE validation, so the error says
#: "identity is not model-supplied" rather than pydantic's generic "extra field",
#: and so the attempt is logged as what it is.
IDENTITY_FIELDS = frozenset(
    {
        "student_id", "user_id", "username", "sub",
        "offering_id", "course_id", "tenant",
        "claims", "group_tokens", "role", "roles",
    }
)
