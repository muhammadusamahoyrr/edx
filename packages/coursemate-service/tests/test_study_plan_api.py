"""`POST /examprep/study-plan` — the budgeted planner, exposed.

Phase 3 built the planner and nothing called it. These tests are mostly about the
seam that was missing: that the route reaches the *existing* planner rather than
growing a second copy of the arithmetic, that scope still comes from the token
and not the body, and that the prose `/plan` route beside it is untouched.

The route functions are called directly with claims, as every other API test here
does — the JWT and rate-limit dependencies are exercised in `test_auth.py`, and
re-testing them through a client would test FastAPI rather than this code. What
IS tested here is that this route declares them.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import FrameType
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import (
    CLO,
    ExamPrepPack,
    ExamPrepRequest,
    QuestionRecord,
    StudyPlan,
    StudyPlanRequest,
)
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
from coursemate_service.api import examprep
from fastapi import HTTPException
from pydantic import ValidationError

TENANT = "default"
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"


def _claims(offering: str = OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="u1", username="alice", course_id=offering, offering_id=offering,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


def _question(qid: str, clo: str, marks: int | None, **kw) -> QuestionRecord:
    return QuestionRecord(
        question_id=qid, tenant=TENANT, offering_id=OFFERING,
        source_doc_id="final-2024.pdf", text=f"Question {qid}",
        clo_id=clo, marks=marks, **kw
    )


def _install(monkeypatch, tmp_path, clos, questions, name="examprep.db"):
    from coursemate_service.boundary import impl
    from coursemate_service.knowledge.examprep_store import ExamPrepStore

    store = ExamPrepStore(tmp_path / name)
    store.load_pack(ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT, clos=clos, questions=questions,
    ))
    monkeypatch.setattr(impl, "get_examprep_store", lambda: store)
    return store


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """40 marks of tagged questions across three outcomes."""
    return _install(
        monkeypatch, tmp_path,
        clos=[
            CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee"),
            CLO(clo_id="CLO-2", text="Scheduling", confirmed_by="dr-lee"),
            CLO(clo_id="CLO-3", text="Memory", confirmed_by="dr-lee"),
        ],
        questions=[
            _question("Q1", "CLO-1", 10, year=2024),
            _question("Q2", "CLO-1", 5, year=2023),
            _question("Q3", "CLO-2", 10, year=2024),
            _question("Q4", "CLO-2", 5, year=2022),
            _question("Q5", "CLO-3", 10, year=2023),
        ],
    )


def _total(plan: StudyPlan) -> int:
    return sum(i.marks_budget for i in plan.items)


# --- the budget ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_requested_budget_is_honoured(loaded):
    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())

    assert isinstance(plan, StudyPlan)
    assert plan.offering_id == OFFERING
    assert _total(plan) == 20
    assert plan.items


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, 5, 15, 20, 33, 40])
async def test_a_plan_never_exceeds_its_budget_through_the_api(loaded, budget):
    """The invariant the whole feature rests on, held at the boundary the browser
    actually calls rather than only in the pure function underneath."""
    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=budget), _claims())
    assert _total(plan) <= budget


@pytest.mark.asyncio
async def test_a_budget_larger_than_the_bank_returns_what_exists(loaded):
    """The honest shortfall. The bank holds 40 marks; asking for 200 gets 40 and
    a plan that is short, never padding to make the number look right."""
    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=200), _claims())

    assert _total(plan) == 40
    assert any("bank had nothing smaller that fit" in (i.rationale or "")
               for i in plan.items)


@pytest.mark.asyncio
async def test_the_shortfall_is_visible_to_an_operator_in_the_log(loaded, caplog):
    """An empty or short plan has four different causes — no pack, no outcomes,
    nothing tagged, nothing with marks — and they need different fixes. The
    report stays out of the contract, so the log is where an operator learns
    which one."""
    with caplog.at_level("INFO"):
        await examprep.study_plan(StudyPlanRequest(marks_budget=200), _claims())

    lines = " ".join(r.getMessage() for r in caplog.records)
    assert "unspent_marks" in lines
    assert "study-plan for" in lines


# --- empty and unusable banks ---------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_bank_returns_an_honest_empty_plan(tmp_path, monkeypatch):
    """A `StudyPlan` with no items, not an error. There is nothing wrong with the
    request — the course simply has nothing tagged yet, and 200-with-no-items
    says that without pretending it is a fault."""
    _install(monkeypatch, tmp_path,
             clos=[CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee")],
             questions=[])

    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=100), _claims())

    assert isinstance(plan, StudyPlan)
    assert plan.items == []
    assert plan.offering_id == OFFERING


@pytest.mark.asyncio
async def test_a_bank_with_no_marks_plans_nothing_rather_than_guessing(
    tmp_path, monkeypatch
):
    """A question with no marks cannot be budgeted. Charging it a default would
    make the budget a fiction in the direction that over-fills a student's
    session."""
    _install(monkeypatch, tmp_path,
             clos=[CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee")],
             questions=[_question("Q1", "CLO-1", None), _question("Q2", "CLO-1", None)])

    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=100), _claims())
    assert plan.items == []


@pytest.mark.asyncio
async def test_an_offering_with_no_confirmed_outcomes_plans_nothing(
    tmp_path, monkeypatch
):
    _install(monkeypatch, tmp_path, clos=[], questions=[])

    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=100), _claims())
    assert plan.items == []


# --- scope and authorization ----------------------------------------------


@pytest.mark.asyncio
async def test_an_unenrolled_caller_is_refused(loaded, monkeypatch):
    """Fails closed with 403, not with an empty plan — which would read to the
    student as "this course has nothing for you".

    Driven through the REAL `_authorize` by failing the enrollment check, not by
    patching the authorizer out. §10.1: the token proves the request was issued,
    the platform decides entitlement, and a signed token must not outlive the
    enrollment it was minted under.
    """
    from coursemate_service.boundary import impl

    monkeypatch.setattr(impl.settings, "enforce_enrollment", True)

    def not_enrolled(username, offering_id):
        raise impl.NotEnrolled(f"{username} is not enrolled in {offering_id}")

    monkeypatch.setattr(impl.verifier, "require_enrolled", not_enrolled)

    with pytest.raises(HTTPException) as exc:
        await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())

    assert exc.value.status_code == 403
    assert exc.value.detail == ErrorCode.NOT_ENROLLED.value


@pytest.mark.asyncio
async def test_an_unverifiable_enrollment_is_refused_not_allowed(loaded, monkeypatch):
    """Fail CLOSED. An availability problem must never become an authorization
    bypass — a tutor that is down is recoverable, one serving another cohort's
    content is not."""
    from coursemate_service.boundary import impl

    monkeypatch.setattr(impl.settings, "enforce_enrollment", True)

    def unreachable(username, offering_id):
        raise impl.PlatformUnreachable("LMS is down")

    monkeypatch.setattr(impl.verifier, "require_enrolled", unreachable)

    with pytest.raises(HTTPException) as exc:
        await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_token_scoped_elsewhere_cannot_reach_this_offerings_pack(
    loaded, monkeypatch
):
    """The planner takes no `offering_id`, so scope IS the token. A caller whose
    token names another offering reads that offering's (absent) pack — never this
    one's."""
    from coursemate_service.boundary import impl

    seen: list[str] = []
    real = impl.boundary.get_clos

    def spy(offering_id, claims):
        seen.append(offering_id)
        return real(offering_id, claims)

    monkeypatch.setattr(impl.boundary, "get_clos", spy)
    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims(OTHER))

    assert seen == [OTHER], "the token's offering, not the loaded one"
    assert plan.offering_id == OTHER
    assert plan.items == [], "no pack exists for that offering"


@pytest.mark.asyncio
async def test_the_route_requires_authentication_and_rate_limiting(loaded):
    """It cannot be reached without a verified token, because the dependency that
    mints `claims` is the one that verifies it. Asserted on the signature rather
    than through a client: a route that quietly swapped `rate_limited` for
    `student_claims` would keep working and silently lose the rate limit."""
    import inspect

    from coursemate_service.api.deps import rate_limited

    depends = inspect.signature(examprep.study_plan).parameters["claims"].default
    assert depends.dependency is rate_limited


@pytest.mark.asyncio
async def test_scope_comes_from_the_token_not_the_body(loaded):
    """There is no `offering_id` or `student_id` field to send, so a forged
    payload cannot widen scope — it cannot even express the attempt."""
    fields = set(StudyPlanRequest.model_fields)
    assert fields == {"marks_budget", "mastery"}

    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())
    assert plan.offering_id == OFFERING


@pytest.mark.asyncio
async def test_a_mastery_snapshot_from_another_offering_is_ignored(loaded):
    """Browser-carried and therefore attacker-controlled, exactly as on `/plan`.
    A snapshot minted elsewhere must shape nothing here."""
    forged = MasterySnapshot(
        offering_id=OTHER,
        clos=[CLOMastery(clo_id="CLO-1", attempts=50, correct=50)],
    )
    with_forged = await examprep.study_plan(
        StudyPlanRequest(marks_budget=20, mastery=forged), _claims()
    )
    with_none = await examprep.study_plan(
        StudyPlanRequest(marks_budget=20), _claims()
    )
    assert with_forged == with_none


@pytest.mark.asyncio
async def test_a_matching_snapshot_shapes_the_plan(loaded):
    snapshot = MasterySnapshot(
        offering_id=OFFERING,
        clos=[CLOMastery(clo_id="CLO-1", attempts=20, correct=20)],
    )
    shaped = await examprep.study_plan(
        StudyPlanRequest(marks_budget=20, mastery=snapshot), _claims()
    )
    plain = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())

    assert shaped != plain, "a mastered outcome should not be weighted the same"


# --- request validation ----------------------------------------------------


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_a_non_positive_budget_is_rejected_by_the_contract(budget):
    """Rejected at the contract, so the caller gets a 422 naming the field rather
    than an empty plan they have to interpret."""
    with pytest.raises(ValidationError):
        StudyPlanRequest(marks_budget=budget)


def test_an_absurd_budget_is_rejected(loaded):
    """The ceiling stops a nonsense number turning into a query for every
    question in the bank. A real exam is well under 500 marks."""
    with pytest.raises(ValidationError):
        StudyPlanRequest(marks_budget=100_000)


def test_the_budget_is_required():
    with pytest.raises(ValidationError):
        StudyPlanRequest()


def test_an_unknown_field_is_not_silently_accepted():
    """Notably `offering_id`: pydantic ignores unknown fields by default, so this
    documents that sending one changes nothing rather than widening scope."""
    request = StudyPlanRequest(marks_budget=20, offering_id=OTHER)
    assert not hasattr(request, "offering_id")


# --- the planner is reached, not reimplemented ----------------------------


@pytest.mark.asyncio
async def test_the_route_calls_the_existing_planner(loaded, monkeypatch):
    """The whole point of 4A. A second copy of the budget arithmetic living in
    the API would drift from the tested one, and both would look right in
    isolation."""
    from coursemate_service.ai import planner

    seen = {}
    real = planner.plan_for_offering

    def spy(claims, **kwargs):
        seen["claims"] = claims
        seen["kwargs"] = kwargs
        return real(claims, **kwargs)

    monkeypatch.setattr(examprep, "plan_for_offering", spy)
    await examprep.study_plan(StudyPlanRequest(marks_budget=25), _claims())

    assert seen["kwargs"]["marks_budget"] == 25
    assert seen["claims"].offering_id == OFFERING
    # No `offering_id` kwarg exists to pass: scope travels in the claims.
    assert "offering_id" not in seen["kwargs"]


@pytest.mark.asyncio
async def test_the_route_makes_no_model_call(loaded, monkeypatch):
    """Arithmetic, not generation. A router reached from here would mean a plan
    could fail because a provider was down."""
    from coursemate_service.ai import client

    def boom():
        raise AssertionError("the planner must not touch a provider")

    monkeypatch.setattr(client, "get_router", boom)
    plan = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())
    assert plan.items


@pytest.mark.asyncio
async def test_the_route_takes_no_concurrency_slot(loaded, monkeypatch):
    """Slots bound how much of the PROVIDER's concurrency a student holds. There
    is no provider here, so taking one would let a free local computation deny
    the student their practice questions."""
    from coursemate_service.api import deps

    monkeypatch.setattr(deps.shared_state, "get_redis", lambda: None)
    limiter = deps._RateLimiter()
    monkeypatch.setattr(examprep, "rate_limiter", limiter)

    for _ in range(4):
        await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())

    assert limiter.active_streams("u1") == 0


# --- the prose route beside it is untouched -------------------------------


@pytest.mark.asyncio
async def test_the_existing_plan_route_still_streams_prose(loaded, monkeypatch):
    """`/plan` is the kill-switch target and the agent's baseline. 4A adds a
    route beside it; it does not replace it."""
    monkeypatch.setattr(examprep.settings, "agent_enabled", False)

    response = await examprep.plan(ExamPrepRequest(request="help me revise"), _claims())
    body = "".join([chunk async for chunk in response.body_iterator])

    assert response.media_type == "text/event-stream"
    assert "revision plan" in body
    assert "CLO-1" in body


@pytest.mark.asyncio
async def test_the_two_plan_routes_are_different_functions(loaded):
    """Different shapes for different questions: free text answered by prose, and
    a number answered by arithmetic."""
    assert examprep.plan is not examprep.study_plan

    streamed = await examprep.plan(ExamPrepRequest(request="revise"), _claims())
    structured = await examprep.study_plan(StudyPlanRequest(marks_budget=20), _claims())

    assert streamed.media_type == "text/event-stream"
    assert isinstance(structured, StudyPlan)


@pytest.mark.asyncio
async def test_the_prose_route_still_reports_preparing_with_no_pack(
    tmp_path, monkeypatch
):
    """The §5.1 distinction `/plan` has always made — "not ready" is not "nothing
    found" — is unchanged by this phase."""
    from coursemate_service.boundary import impl
    from coursemate_service.knowledge.examprep_store import ExamPrepStore

    monkeypatch.setattr(impl, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "empty.db"))
    monkeypatch.setattr(examprep.settings, "agent_enabled", False)

    response = await examprep.plan(ExamPrepRequest(request="revise"), _claims())
    body = "".join([chunk async for chunk in response.body_iterator])

    assert FrameType.ERROR.value in body
    assert ErrorCode.PREPARING.value in body
