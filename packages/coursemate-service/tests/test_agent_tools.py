"""The tool handlers against real stores.

`test_agent_registry.py` checks what the registry refuses. This checks what the
handlers do once a call is admitted — with a real chunk index and a real
past-paper pack behind them, because the properties that matter (the gate fires
per call, a denied scope returns nothing, gated chunks are unreachable) are
properties of the data path, not of the argument parsing.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.examprep import CLO, ExamPrepPack, ExamType, QuestionRecord
from coursemate_service.agents import tools as agent_tools  # noqa: F401 — registers
from coursemate_service.agents.registry import ToolContext, ToolStatus, registry
from coursemate_service.knowledge.examprep_store import ExamPrepStore
from coursemate_service.knowledge.store import ChunkStore

TENANT = "default"
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"

_CHUNKS = [
    ("Deadlock avoidance",
     "A deadlock arises when processes hold resources and wait on each other in "
     "a circular chain. Deadlock avoidance uses the banker's algorithm."),
    ("Round robin scheduling",
     "Round robin scheduling gives each process a fixed time quantum in turn."),
    ("Course welcome",
     "Welcome to the demonstration course. This page introduces the syllabus."),
]


def _claims(offering: str = OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="42", username="alice", course_id=offering, offering_id=offering,
        exp=now + 300, iat=now,
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A real index and a real pack, wired in where the boundary looks."""
    chunks = ChunkStore(tmp_path / "index.db")
    chunks.write_chunks([
        {
            "tenant": TENANT, "course_id": OFFERING, "offering_id": OFFERING,
            "usage_key": f"block-v1:{i}", "block_id": f"b{i}", "block_type": "html",
            "content_type": "text", "display_name": name, "version": "v1",
            "ordinal": i, "text": text,
        }
        for i, (name, text) in enumerate(_CHUNKS)
    ])
    chunks.swap(OFFERING, "v1")

    exams = ExamPrepStore(tmp_path / "examprep.db")
    exams.load_pack(ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=[CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee"),
              CLO(clo_id="CLO-2", text="Scheduling")],
        questions=[
            QuestionRecord(question_id="Q1", tenant=TENANT, offering_id=OFFERING,
                           source_doc_id="final-2024.pdf", text="Explain deadlock.",
                           clo_id="CLO-1", year=2024, marks=10,
                           exam_type=ExamType.FINAL, difficulty=0.7),
            QuestionRecord(question_id="Q2", tenant=TENANT, offering_id=OFFERING,
                           source_doc_id="quiz-2019.pdf", text="Define a time quantum.",
                           clo_id="CLO-2", year=2019, marks=2, exam_type=ExamType.QUIZ),
        ],
    ))

    import coursemate_service.boundary.impl as impl

    monkeypatch.setattr(impl, "get_store", lambda: chunks)
    monkeypatch.setattr(impl, "get_examprep_store", lambda: exams)
    return impl


def call(name, args=None, offering=OFFERING):
    return registry.invoke(name, args or {}, ToolContext(claims=_claims(offering)))


# --- search_course_content: the gate runs per call -------------------------


def test_a_matching_query_returns_labelled_chunks(wired):
    result = call("search_course_content", {"query": "deadlock circular chain"})
    assert result.status is ToolStatus.OK
    chunks = result.data["chunks"]
    assert chunks and chunks[0]["display_name"] == "Deadlock avoidance"
    # Ordinal labels, not usage keys: a model that invents `[7]` when four chunks
    # were supplied produces a citation that resolves to nothing and is caught,
    # where an invented usage key might look plausible.
    assert [c["label"] for c in chunks] == list(range(1, len(chunks) + 1))


def test_an_off_topic_query_is_gated_not_answered_thinly(wired):
    """The measured 0-false-answer property. Below the bar the tool returns
    nothing at all, so there is no chunk available to be cited."""
    result = call("search_course_content",
                  {"query": "explain quantum chromodynamics and gluon confinement"})
    assert result.status is ToolStatus.GATED
    assert result.data["chunks"] == []
    assert result.gate_applied is True


def test_an_unindexed_offering_says_preparing_not_not_covered(wired):
    """§5.1: two different sentences to a student, and only one of them invites
    them back. The message must not be 'this course does not cover it'."""
    result = call("search_course_content", {"query": "deadlock"}, offering=OTHER)
    assert result.status is ToolStatus.GATED
    assert "still being prepared" in result.message
    assert result.gate_applied is True


def test_the_gate_uses_the_configured_threshold(wired, monkeypatch):
    """Not a threshold of its own. A tool with a private bar would abstain
    differently from the chat path, on questions nobody tested, and both would
    look correct in isolation."""
    from coursemate_service.ai import gate

    monkeypatch.setattr(gate.settings, "confidence_threshold", 0.99)
    assert call("search_course_content", {"query": "deadlock"}).status is ToolStatus.GATED

    monkeypatch.setattr(gate.settings, "confidence_threshold", 0.0)
    assert call("search_course_content", {"query": "deadlock"}).status is ToolStatus.OK


def test_grounding_disabled_bypasses_the_gate_on_both_paths(wired, monkeypatch):
    """`require_grounding` has to mean the same thing here as in chat. A flag
    that governed one path and not the other would be worse than no flag."""
    from coursemate_service.ai import gate

    monkeypatch.setattr(gate.settings, "require_grounding", False)
    result = call("search_course_content", {"query": "quantum chromodynamics"})
    assert result.status is ToolStatus.OK


# --- search_past_questions -------------------------------------------------


def test_structured_filters_reach_the_store(wired):
    """The §7.6 claim, end to end through the tool rather than only the store."""
    result = call("search_past_questions",
                  {"clo_id": "CLO-1", "earliest_year": 2023, "minimum_marks": 5})
    assert [q["question_id"] for q in result.data["questions"]] == ["Q1"]
    assert result.data["filters_applied"]["earliest_year"] == 2023


def test_a_derived_difficulty_reaches_the_model_labelled(wired):
    """§7.6 requires a derived difficulty to be labelled wherever it is shown,
    and a field the model never sees cannot be labelled by it."""
    q = call("search_past_questions", {"clo_id": "CLO-1"}).data["questions"][0]
    assert q["difficulty"] == 0.7
    assert q["difficulty_is_derived"] is True


def test_no_match_is_ok_with_advice_not_an_error(wired):
    """The filter was valid; nothing matched. Telling the model to relax a
    constraint beats leaving it to guess whether the tool is broken."""
    result = call("search_past_questions", {"earliest_year": 2099})
    assert result.status is ToolStatus.OK
    assert result.data["questions"] == []
    assert "relaxing" in result.message


def test_an_offering_with_no_pack_is_gated_but_not_gate_applied(wired):
    """'No past papers loaded' is an empty answer, not a confidence failure — so
    it must not abstain the whole turn."""
    result = call("search_past_questions", {}, offering=OTHER)
    assert result.status is ToolStatus.GATED
    assert result.gate_applied is False


# --- get_plan_context (the merged tool) -------------------------------------


def test_clos_carry_their_confirmation_state(wired):
    """§7.3: extraction is assisted, never asserted. An unconfirmed list is
    usable but must not be presented as the instructor's."""
    clos = {c["clo_id"]: c["confirmed"] for c in call("get_plan_context").data["clos"]}
    assert clos == {"CLO-1": True, "CLO-2": False}


def test_clos_for_an_unloaded_offering_are_empty_not_an_error(wired):
    result = call("get_plan_context", {}, offering=OTHER)
    assert result.status is ToolStatus.OK
    assert result.data["clos"] == []


def test_one_call_returns_everything_a_plan_needs(wired):
    """The merge, checked as a whole. Outcomes, history and searchability arrive
    together — the point is that no second round trip is needed to rank what to
    revise, and a round trip costs ~26 s on the local model."""
    data = call("get_plan_context").data
    assert [c["clo_id"] for c in data["clos"]] == ["CLO-1", "CLO-2"]
    assert data["mastery_known"] is False          # no snapshot on this request
    assert data["past_papers_available"] is True   # saves a wasted search round


def test_it_reports_when_there_is_nothing_to_search(wired):
    """Knowing the bank is empty up front saves an entire round trip, which is
    the same reasoning that motivated the merge."""
    assert call("get_plan_context", {}, offering=OTHER).data["past_papers_available"] is False


# --- cross-offering, through the tool surface ------------------------------


def test_the_boundary_refuses_a_direct_cross_offering_call(wired):
    """The second layer. A future caller that is not a tool still cannot ask for
    an offering the token does not cover."""
    from coursemate_service.boundary.impl import AuthorizationError, boundary

    intruder = _claims(OTHER)
    for method, args in (
        (boundary.retrieve_course_context, ("deadlock", OFFERING, intruder)),
        (boundary.search_past_questions, (OFFERING, intruder)),
        (boundary.get_clos, (OFFERING, intruder)),
    ):
        with pytest.raises(AuthorizationError):
            method(*args)


def test_no_tool_can_even_express_a_cross_offering_request(wired):
    """The first layer, and the stronger one: every tool reads the offering from
    `ctx.claims`, and no schema has a field for it. So a token scoped to another
    offering does not get refused so much as it gets *its own* offering — which
    here holds nothing.

    Worth stating as its own test, because the two layers fail differently. If
    someone later adds an `offering_id` argument "for flexibility", this test
    keeps passing while the property it names quietly stops being true — so it
    asserts the schema shape, not only the empty result."""
    intruder = _claims(OTHER)
    for name in ("search_course_content", "search_past_questions", "get_plan_context"):
        args = {"query": "deadlock"} if name == "search_course_content" else {}
        result = registry.invoke(name, args, ToolContext(claims=intruder))
        assert result.status is not ToolStatus.ERROR
        assert not any(result.data.values()), name

    for schema in registry.schemas():
        properties = set(schema["parameters"].get("properties", {}))
        assert "offering_id" not in properties
        assert "course_id" not in properties


def test_the_model_facing_names_are_the_ones_models_reach_for(wired):
    """Groq validates tool arguments server-side against `additionalProperties:
    false`, so ONE invented parameter name kills the entire request before the
    registry sees it. llama-3.3-70b invented `earliest_year` and `minimum_marks`
    against the terser `year_from`/`min_marks`, and every turn died.

    The schema now uses the names the model reaches for, and the terse internal
    names survive unchanged behind `tools.py`. This pins both halves: the new
    names work, and the old ones are refused rather than silently ignored."""
    ok = call("search_past_questions", {"earliest_year": 2023, "minimum_marks": 5})
    assert ok.status is ToolStatus.OK

    stale = call("search_past_questions", {"year_from": 2023})
    assert stale.status is ToolStatus.ERROR
    assert "year_from" in stale.message
