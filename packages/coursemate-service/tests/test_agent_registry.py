"""The tool registry's security properties.

Every test here fails against a registry that "just works" — one that validates
loosely, or overrides model-supplied identity instead of refusing it, or lets a
handler exception escape. Those are the three ways this layer goes wrong quietly.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
from coursemate_service.agents import tools as _tools  # noqa: F401 — registers tools
from coursemate_service.agents.registry import (
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    registry,
)
from coursemate_service.agents.schemas import IDENTITY_FIELDS, SearchCourseContentArgs

OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"


def claims(offering: str = OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="42", username="alice", course_id=offering, offering_id=offering,
        exp=now + 300, iat=now,
    )


def ctx(mastery: MasterySnapshot | None = None) -> ToolContext:
    return ToolContext(claims=claims(), mastery=mastery)


# --- identity is injected, never accepted --------------------------------


@pytest.mark.parametrize("field", sorted(IDENTITY_FIELDS))
def test_every_identity_field_is_refused(field):
    """Not "ignored" — refused, with the attempt named.

    Silently dropping the field would make a prompt-injection attempt
    indistinguishable from a clean call in every log we keep.
    """
    result = registry.invoke(
        "search_course_content", {"query": "deadlock", field: "someone-else"}, ctx()
    )
    assert result.status is ToolStatus.ERROR
    assert field in result.message


def test_refusal_names_the_field_but_not_its_value():
    """The value is attacker-controlled and must not be reflected into an
    operator-facing message; the field name is what an operator can act on."""
    poison = "course-v1:Evil+Corp+Steal"
    result = registry.invoke(
        "search_course_content", {"query": "x", "offering_id": poison}, ctx()
    )
    assert result.status is ToolStatus.ERROR
    assert "offering_id" in result.message
    assert poison not in result.message


def test_identity_refusal_beats_generic_validation():
    """`extra="forbid"` would also reject `offering_id`, but as an anonymous
    'unexpected field'. The identity check must run first so the two are
    distinguishable — they warrant different operator responses."""
    result = registry.invoke("get_plan_context", {"offering_id": OFFERING}, ctx())
    assert result.status is ToolStatus.ERROR
    assert "Identity and course scope come from the student's session" in result.message


# --- strict schemas ------------------------------------------------------


def test_unknown_argument_is_rejected_not_dropped():
    result = registry.invoke(
        "search_course_content", {"query": "x", "sort_by": "relevance"}, ctx()
    )
    assert result.status is ToolStatus.ERROR
    assert "sort_by" in result.message


def test_out_of_range_limit_is_rejected_before_execution():
    """An agent asking for 10,000 results is a cost event. 'The model asked for
    it' is not a reason, and the ceiling is enforced by the schema so no handler
    has to remember it."""
    result = registry.invoke("search_course_content", {"query": "x", "limit": 9999}, ctx())
    assert result.status is ToolStatus.ERROR
    assert "limit" in result.message


def test_schema_forbids_additional_properties():
    """Providers with strict function calling enforce this on their side, so a
    well-behaved provider never sends an invented argument at all."""
    for schema in registry.schemas():
        assert schema["parameters"]["additionalProperties"] is False


def test_no_schema_declares_an_identity_field():
    """The structural half of decision 2: the model is never even shown a field
    it could put identity in."""
    for schema in registry.schemas():
        assert not (IDENTITY_FIELDS & set(schema["parameters"].get("properties", {})))


def test_malformed_arguments_do_not_raise():
    assert registry.invoke("get_plan_context", "not-a-dict", ctx()).status is ToolStatus.ERROR


@pytest.mark.parametrize(
    "raw", ['{"clo_id": ', "not json at all", "[1, 2]", '"a string"', "5"],
)
def test_garbled_arguments_never_reach_a_tool_as_empty_ones(raw):
    """The defect lived in the JOIN, so the test has to cross it.

    `_decode_args` returned `None` on a truncated JSON string; `invoke` did
    `args = raw_args or {}`; and `get_plan_context` — which legitimately takes no
    arguments — then executed normally. A garbled tool call produced a clean
    result and nothing anywhere recorded that the model had malfunctioned. That
    is the failure-path-returns-success shape this project keeps finding.

    Testing `invoke(UNPARSEABLE)` alone does NOT catch it: the sentinel is a
    truthy non-dict, so even the broken `raw_args or {}` rejects it. Only the
    decode-then-invoke path reproduces the bug, which is why this test calls
    both — verified by reverting the fix and watching it fail.
    """
    from coursemate_service.agents.runner import _decode_args

    result = registry.invoke("get_plan_context", _decode_args(raw), ctx())
    assert result.status is ToolStatus.ERROR, f"{raw!r} was accepted as empty arguments"


def test_genuinely_absent_arguments_are_still_accepted():
    """The other half. Several tools take no arguments, and a provider that sends
    `""` or omits the field must not be treated as malfunctioning."""
    from coursemate_service.agents.runner import _decode_args

    for raw in (None, "", "{}"):
        assert registry.invoke("get_plan_context", _decode_args(raw), ctx()).status \
            is not ToolStatus.ERROR, repr(raw)


# --- failures are results, never exceptions -------------------------------


def test_unknown_tool_returns_an_error_result_listing_the_real_ones():
    result = registry.invoke("delete_everything", {}, ctx())
    assert result.status is ToolStatus.ERROR
    assert "get_plan_context" in result.message


def test_handler_exception_does_not_leak_its_text_to_the_model():
    """A handler's exception can carry SQL, file paths and other tenants'
    offering ids. The operator gets the traceback; the model gets a fact."""
    secret = "/data/other-tenant/index.db is locked"

    def boom(args, ctx):
        raise RuntimeError(secret)

    local = ToolRegistry()
    local.register(Tool("boom", "d", SearchCourseContentArgs, boom))
    result = local.invoke("boom", {"query": "x"}, ctx())

    assert result.status is ToolStatus.ERROR
    assert secret not in result.message


def test_duplicate_registration_is_refused():
    """The same failure shape as two LiteLLM deployments sharing a `model_name`:
    everything works, and half the calls go somewhere unintended."""
    local = ToolRegistry()
    tool = Tool("t", "d", SearchCourseContentArgs,
                lambda a, c: ToolResult("t", ToolStatus.OK))
    local.register(tool)
    with pytest.raises(ValueError, match="already registered"):
        local.register(tool)


# --- mastery: empty is an answer, not a failure ---------------------------
#
# Mastery is read through `get_plan_context` since the 2026-08-11 merge — the
# standalone `get_mastery` tool is gone. The properties below are unchanged and
# are re-asserted against the merged tool precisely because a merge is exactly
# where a subtle behaviour change hides.


def test_new_student_with_no_mastery_is_ok_not_error():
    """Decision 7. A runner that treated this as a failure would retry a working
    tool until the iteration cap, then report a fault that never happened."""
    result = registry.invoke("get_plan_context", {}, ctx(mastery=None))
    assert result.status is ToolStatus.OK
    assert result.data["mastery"] == []
    assert result.data["mastery_known"] is False
    assert not result.failed


def test_mastery_arrives_with_the_outcomes_in_one_call():
    """The whole point of the merge: a planner needs both every turn, and two
    tools meant two model round trips at ~26 s each."""
    snapshot = MasterySnapshot(
        offering_id=OFFERING,
        clos=[CLOMastery(clo_id="CLO-1", attempts=4, correct=1),
              CLOMastery(clo_id="CLO-2", attempts=0, correct=0)],
    )
    result = registry.invoke("get_plan_context", {}, ctx(snapshot))
    assert result.status is ToolStatus.OK
    assert {"clos", "mastery", "mastery_known", "past_papers_available"} <= set(result.data)
    assert [m["clo_id"] for m in result.data["mastery"]] == ["CLO-1", "CLO-2"]
    assert result.data["mastery"][0]["accuracy"] == 0.25


def test_untried_clo_reports_null_accuracy_not_zero():
    """0.0 would rank an unattempted outcome identically to one the student has
    failed repeatedly — inverting the recommendation the field exists to make."""
    snapshot = MasterySnapshot(
        offering_id=OFFERING, clos=[CLOMastery(clo_id="CLO-9", attempts=0, correct=0)]
    )
    result = registry.invoke("get_plan_context", {}, ctx(snapshot))
    assert result.data["mastery"][0]["accuracy"] is None


def test_mastery_for_another_offering_is_ignored():
    """The snapshot is browser-carried and therefore attacker-controlled. It may
    shape the student's own plan; it may not import another course's state.

    Re-asserted after the merge: this check used to live in the standalone
    mastery tool, and a merge that dropped it would be silent."""
    snapshot = MasterySnapshot(
        offering_id="course-v1:Other+X+Y",
        clos=[CLOMastery(clo_id="CLO-1", attempts=9, correct=9)],
    )
    result = registry.invoke("get_plan_context", {}, ctx(snapshot))
    assert result.status is ToolStatus.OK
    assert result.data["mastery"] == []
    assert result.data["mastery_known"] is False


def test_the_tool_surface_is_read_only():
    """§10.6's claim — 'there is no prompt that makes CourseMate change what
    students see' — is structural, and this is the structure. A write tool
    appearing here silently ends the claim, so the surface is pinned."""
    assert registry.names() == [
        "get_plan_context", "search_course_content", "search_past_questions",
    ]


def test_schemas_use_the_openai_parameters_key():
    """The runner wraps each schema in `{"type": "function", "function": ...}` —
    the OpenAI envelope — where the schema key is `parameters`.

    This emitted `input_schema`, Anthropic's key, until 2026-08-11. Inside an
    OpenAI envelope that key is unknown, so every provider saw tools with NO
    parameters: Groq rejected every call naming the very fields the schema
    declared, and Ollama silently let the model guess. A strict schema nothing
    upstream enforces is not a control.
    """
    for schema in registry.schemas():
        assert "parameters" in schema, schema["name"]
        assert "input_schema" not in schema, schema["name"]
        assert schema["parameters"]["type"] == "object"


def test_the_declared_arguments_actually_reach_the_wire_shape():
    """Not just "a key exists" — the fields have to be in it. The bug above was
    invisible precisely because the schema object itself was well-formed; it was
    simply filed under a name nothing read."""
    spq = next(s for s in registry.schemas() if s["name"] == "search_past_questions")
    assert {"clo_id", "exam_type", "earliest_year", "minimum_marks", "limit"} <= set(
        spq["parameters"]["properties"]
    )
