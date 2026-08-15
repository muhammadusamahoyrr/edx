"""The deterministic revision plan as a VALUE — `POST /examprep/revision-plan`.

The same plan `/plan` streams with the agent off, carried as structure instead
of as markdown inside text tokens. `/study-plan` next door already argues the
rule: *"a StudyPlan is a value, not a narration."*

**Why this was worth a contract change rather than a client-side parser.** The
markup collided with its own data: `_Source: oex101_final_2024.pdf, p.2_` cannot
be told from its own italics, because the filename contains underscores. A
parser either mangles the filename or refuses the line. Structure removes the
collision instead of working around it, which is why the underscore case below
is a regression test and not a curiosity.

The prose stream stays: with `agent_enabled=True` the agent genuinely narrates,
and the agent-off stream remains the kill-switch fallback CLAUDE.md requires to
keep working.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.examprep import CLO, ExamPrepRequest, QuestionRecord
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot

from coursemate_service.api.plan import (
    PlanUnavailable,
    build_revision_plan,
    deterministic_plan,
)

OFFERING = "course-v1:OpenedX+OEX101+2023"


def make_claims(offering_id: str = OFFERING, sub: str = "student-1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, username="alice", course_id=offering_id, offering_id=offering_id,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


def _q(qid: str, **kw) -> QuestionRecord:
    base = dict(
        question_id=qid, tenant="default", offering_id=OFFERING,
        source_doc_id="oex101_final_2024.pdf", text=f"Question {qid}",
        page=2, marks=3, year=2024, clo_id="CLO-1",
    )
    return QuestionRecord(**{**base, **kw})


class _Boundary:
    """Stand-in for the CourseIntelligence boundary."""

    def __init__(self, clos, questions, *, pack=True):
        self._clos, self._questions, self._pack = clos, questions, pack

    def has_exam_pack(self, offering_id):
        return self._pack

    def get_clos(self, offering_id, claims):
        return self._clos

    def search_past_questions(self, offering_id, claims, *, clo_id=None, limit=10):
        return self._questions.get(clo_id, [])


@pytest.fixture
def patched(monkeypatch):
    def _install(clos, questions, *, pack=True):
        from coursemate_service.api import plan as mod

        monkeypatch.setattr(mod, "boundary", _Boundary(clos, questions, pack=pack))
        return mod

    return _install


CLOS = [
    CLO(clo_id="CLO-1", text="Identify the organisations and roles"),
    CLO(clo_id="CLO-2", text="Explain the named release process"),
]


# --- the structure carries everything the prose did ------------------------


def test_every_outcome_is_returned_with_its_text_and_questions(patched):
    patched(CLOS, {"CLO-1": [_q("q1"), _q("q2")], "CLO-2": [_q("q3", clo_id="CLO-2")]})

    plan = build_revision_plan(ExamPrepRequest(request="what should I revise?"),
                               make_claims(offering_id=OFFERING))

    assert plan.offering_id == OFFERING
    assert [o.clo_id for o in plan.outcomes] == ["CLO-1", "CLO-2"]
    assert plan.outcomes[0].clo_text == "Identify the organisations and roles"
    assert [q.question_id for q in plan.outcomes[0].questions] == ["q1", "q2"]


def test_the_source_filename_is_carried_verbatim(patched):
    """The regression this contract change exists for.

    As markdown the filename's underscores were indistinguishable from the
    italic markers around it. As a field there is nothing to parse."""
    patched(CLOS[:1], {"CLO-1": [_q("q1", source_doc_id="a_b_c_2024_final_v2.pdf")]})

    plan = build_revision_plan(ExamPrepRequest(request="x"),
                               make_claims(offering_id=OFFERING))

    assert plan.outcomes[0].questions[0].source_doc_id == "a_b_c_2024_final_v2.pdf"


def test_provenance_fields_survive(patched):
    """§7.6: page, marks, year and the low-confidence flag all reach the client,
    because they are already on `QuestionRecord` and nothing re-encodes them."""
    patched(CLOS[:1], {"CLO-1": [_q("q1", page=7, marks=10, year=2023,
                                    low_confidence_flag=True)]})

    q = build_revision_plan(ExamPrepRequest(request="x"),
                            make_claims(offering_id=OFFERING)).outcomes[0].questions[0]

    assert (q.page, q.marks, q.year, q.low_confidence_flag) == (7, 10, 2023, True)


def test_mastery_becomes_counters_not_a_score(patched):
    """`CLOMastery`'s own reasoning: counters can be rescored later, a stored
    score cannot be un-rounded."""
    patched(CLOS[:1], {"CLO-1": [_q("q1")]})
    snap = MasterySnapshot(offering_id=OFFERING,
                           clos=[CLOMastery(clo_id="CLO-1", attempts=3, correct=2)])

    outcome = build_revision_plan(ExamPrepRequest(request="x", mastery=snap),
                                  make_claims(offering_id=OFFERING)).outcomes[0]

    assert (outcome.attempts, outcome.correct) == (3, 2)


def test_an_unpractised_outcome_reports_zero_attempts(patched):
    """Zero attempts is "not practised yet", which the client renders
    differently from "0 correct" — a different statement about the student."""
    patched(CLOS[:1], {"CLO-1": [_q("q1")]})

    outcome = build_revision_plan(ExamPrepRequest(request="x"),
                                  make_claims(offering_id=OFFERING)).outcomes[0]

    assert (outcome.attempts, outcome.correct) == (0, 0)


def test_an_outcome_with_no_questions_is_present_and_empty(patched):
    """Not an error and not omitted. The student is told the outcome exists and
    has nothing tagged, which is a fact about the course, not a fault."""
    patched(CLOS[:1], {})

    plan = build_revision_plan(ExamPrepRequest(request="x"),
                               make_claims(offering_id=OFFERING))

    assert len(plan.outcomes) == 1
    assert plan.outcomes[0].questions == []


def test_a_snapshot_for_another_offering_is_ignored(patched):
    """Browser-carried and therefore attacker-controlled. Checked, not trusted —
    unchanged from the streaming path."""
    patched(CLOS[:1], {"CLO-1": [_q("q1")]})
    foreign = MasterySnapshot(offering_id="course-v1:Other+X+1",
                              clos=[CLOMastery(clo_id="CLO-1", attempts=9, correct=9)])

    outcome = build_revision_plan(ExamPrepRequest(request="x", mastery=foreign),
                                  make_claims(offering_id=OFFERING)).outcomes[0]

    assert outcome.attempts == 0, "another offering's mastery shaped this plan"


# --- the failure states stay distinct (§5.1) --------------------------------


def test_no_pack_is_preparing_not_a_fault(patched):
    patched([], {}, pack=False)
    with pytest.raises(PlanUnavailable) as exc:
        build_revision_plan(ExamPrepRequest(request="x"),
                            make_claims(offering_id=OFFERING))
    assert exc.value.code.value == "preparing"


def test_no_outcomes_is_preparing(patched):
    patched([], {})
    with pytest.raises(PlanUnavailable) as exc:
        build_revision_plan(ExamPrepRequest(request="x"),
                            make_claims(offering_id=OFFERING))
    assert exc.value.code.value == "preparing"


# --- one selection, two presentations --------------------------------------


async def test_the_stream_and_the_structure_agree_on_what_to_revise(patched):
    """The design guard. Two selections meant to agree would drift, and the
    drift would surface as the same student being told to revise different
    outcomes depending on which surface they opened."""
    patched(CLOS, {"CLO-1": [_q("q1")], "CLO-2": [_q("q3", clo_id="CLO-2")]})
    req, claims = ExamPrepRequest(request="x"), make_claims(offering_id=OFFERING)

    structured = [o.clo_id for o in build_revision_plan(req, claims).outcomes]

    prose = ""
    async for frame in deterministic_plan(req, claims):
        if frame.type.value == "token":
            prose += frame.text or ""

    for clo_id in structured:
        assert f"## {clo_id}" in prose, f"{clo_id} is in the data but not the prose"
    assert prose.index("## CLO-1") < prose.index("## CLO-2"), "orders disagree"


async def test_the_stream_still_reports_the_same_failure_states(patched):
    """The kill-switch path keeps working. `deterministic_plan` now renders the
    shared selection, so its error frames come from the same source of truth."""
    patched([], {}, pack=False)

    frames = [f async for f in deterministic_plan(
        ExamPrepRequest(request="x"), make_claims(offering_id=OFFERING))]

    assert frames[0].type.value == "error"
    assert frames[0].error_code.value == "preparing"
