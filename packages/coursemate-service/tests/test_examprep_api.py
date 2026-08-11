"""Feature B end to end at the service: the kill switch, the deterministic path,
and per-tool authorization.

The kill switch is the important one. A flag that routes to a broken or empty
path is a flag nobody dares turn off, which makes it useless as a control — so
"the deterministic path actually answers" is tested, not assumed.
"""

from __future__ import annotations

import json
import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import FrameType
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import CLO, ExamPrepPack, ExamPrepRequest, QuestionRecord
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
from coursemate_service.boundary.impl import AuthorizationError, boundary
from coursemate_service.knowledge.examprep_store import ExamPrepStore

TENANT = "default"
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"


def _claims(offering: str = OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="u1", username="alice", course_id=offering, offering_id=offering,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


def _question(qid: str, clo: str, **kw) -> QuestionRecord:
    return QuestionRecord(
        question_id=qid, tenant=TENANT, offering_id=OFFERING,
        source_doc_id="final-2024.pdf", text=f"Question {qid}", clo_id=clo, **kw
    )


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """A store with a small pack, wired in where the boundary looks for it."""
    store = ExamPrepStore(tmp_path / "examprep.db")
    store.load_pack(ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=[
            CLO(clo_id="CLO-1", text="Concurrency", confirmed_by="dr-lee"),
            CLO(clo_id="CLO-2", text="Scheduling", confirmed_by="dr-lee"),
            CLO(clo_id="CLO-3", text="Memory", confirmed_by="dr-lee"),
        ],
        questions=[
            _question("Q1", "CLO-1", year=2024, marks=10, page=3),
            _question("Q2", "CLO-2", year=2023, marks=5),
            _question("Q3", "CLO-3", year=2022, marks=8, low_confidence_flag=True),
        ],
    ))
    import coursemate_service.boundary.impl as impl

    monkeypatch.setattr(impl, "get_examprep_store", lambda: store)
    return store


async def _collect(source):
    return [f async for f in source]


def _text(frames) -> str:
    return "".join(f.text or "" for f in frames if f.type == FrameType.TOKEN)


# --- per-tool authorization (decision 2) -----------------------------------


def test_every_exam_tool_refuses_a_cross_offering_call(loaded):
    """The boundary is the chokepoint, and each tool goes through it. A tool that
    forgot would return another cohort's papers and look entirely normal doing it."""
    intruder = _claims(OTHER)
    with pytest.raises(AuthorizationError):
        boundary.search_past_questions(OFFERING, intruder)
    with pytest.raises(AuthorizationError):
        boundary.get_clos(OFFERING, intruder)


def test_a_tool_call_is_audited(loaded, caplog):
    """§10.5. An access with no audit record is an access nobody can review."""
    with caplog.at_level("INFO"):
        boundary.get_clos(OFFERING, _claims())
    lines = [r.getMessage() for r in caplog.records]
    assert any("audit tool=get_clos" in line for line in lines)
    # The student's request text is deliberately absent: §3.1 keeps chat and
    # query text out of our logs, and an audit trail records that access
    # happened, not what was asked.
    assert any(f"offering={OFFERING}" in line for line in lines)


# --- the kill switch (decision 1) ------------------------------------------


@pytest.mark.asyncio
async def test_the_flag_off_reaches_the_deterministic_path(loaded, monkeypatch):
    """And no agent code runs. The import is inside the branch precisely so a
    broken agent cannot take down the endpoint routing around it."""
    from coursemate_service.api import examprep

    monkeypatch.setattr(examprep.settings, "agent_enabled", False)

    called = {}
    from coursemate_service.api import plan as plan_mod

    real = plan_mod.deterministic_plan

    def spy(request, claims):
        called["yes"] = True
        return real(request, claims)

    monkeypatch.setattr(plan_mod, "deterministic_plan", spy)
    response = await examprep.plan(ExamPrepRequest(request="help me revise"), _claims())
    body = "".join([chunk async for chunk in response.body_iterator])

    assert called == {"yes": True}
    assert "revision plan" in body
    # SSE framing, not raw text: the browser reuses the chat renderer, and a
    # frame without the blank-line terminator is buffered rather than dispatched.
    assert body.startswith("data: ") and body.endswith("\n\n")


@pytest.mark.asyncio
async def test_the_deterministic_path_is_not_a_stub(loaded):
    """It has to actually answer, or the switch is unusable in practice."""
    from coursemate_service.api.plan import deterministic_plan

    frames = await _collect(deterministic_plan(ExamPrepRequest(request="revise"), _claims()))
    text = _text(frames)

    assert frames[-1].type == FrameType.DONE
    assert "CLO-1" in text and "Question Q1" in text
    assert any(f.type == FrameType.CITATION for f in frames)


@pytest.mark.asyncio
async def test_the_deterministic_path_makes_no_model_call(loaded, monkeypatch):
    """Its whole value is that it works with no provider configured — which is
    also the state a fresh install is in."""
    from coursemate_service.ai import client
    from coursemate_service.api.plan import deterministic_plan

    def explode():
        raise AssertionError("the deterministic path must not reach a provider")

    monkeypatch.setattr(client, "get_router", explode)
    frames = await _collect(deterministic_plan(ExamPrepRequest(request="revise"), _claims()))
    assert frames[-1].type == FrameType.DONE


# --- the ranking rule ------------------------------------------------------


@pytest.mark.asyncio
async def test_unattempted_outcomes_lead_the_plan(loaded):
    """Unknown beats known-weak. Ranking an unattempted CLO as 0% accuracy would
    put it level with one the student has failed repeatedly, and ranking it as
    100% would hide it entirely — both wrong for a revision session."""
    from coursemate_service.api.plan import deterministic_plan

    mastery = MasterySnapshot(offering_id=OFFERING, clos=[
        CLOMastery(clo_id="CLO-1", attempts=10, correct=1),   # known-weak
        CLOMastery(clo_id="CLO-2", attempts=10, correct=9),   # known-strong
        # CLO-3 unattempted
    ])
    frames = await _collect(deterministic_plan(
        ExamPrepRequest(request="revise", mastery=mastery), _claims()
    ))
    text = _text(frames)
    assert text.index("CLO-3") < text.index("CLO-1") < text.index("CLO-2")


@pytest.mark.asyncio
async def test_a_mastery_snapshot_for_another_offering_is_ignored(loaded):
    """Browser-carried, therefore attacker-controlled. It may shape the student's
    own plan; it may not import another course's state."""
    from coursemate_service.api.plan import deterministic_plan

    foreign = MasterySnapshot(offering_id=OTHER, clos=[
        CLOMastery(clo_id="CLO-3", attempts=10, correct=10),
    ])
    frames = await _collect(deterministic_plan(
        ExamPrepRequest(request="revise", mastery=foreign), _claims()
    ))
    # Every CLO is treated as unattempted, so the order falls back to the CLO
    # list's own order rather than to the forged snapshot's.
    assert "not practised yet" in _text(frames)


@pytest.mark.asyncio
async def test_every_question_carries_its_provenance(loaded):
    """§7.6. A practice item a student cannot trace back to a real paper is
    indistinguishable from one we invented."""
    from coursemate_service.api.plan import deterministic_plan

    text = _text(await _collect(
        deterministic_plan(ExamPrepRequest(request="revise"), _claims())
    ))
    assert "Source: final-2024.pdf, p.3" in text


@pytest.mark.asyncio
async def test_low_confidence_extraction_is_shown_not_hidden(loaded):
    from coursemate_service.api.plan import deterministic_plan

    text = _text(await _collect(
        deterministic_plan(ExamPrepRequest(request="revise"), _claims())
    ))
    assert "Extraction confidence was low" in text


@pytest.mark.asyncio
async def test_the_deterministic_plan_never_claims_to_generate_questions(loaded):
    """It quotes past papers verbatim. Saying otherwise would misattribute a real
    exam question to an AI, which is the mirror image of the labelling rule."""
    from coursemate_service.api.plan import deterministic_plan

    text = _text(await _collect(
        deterministic_plan(ExamPrepRequest(request="revise"), _claims())
    ))
    assert "Nothing here is AI-generated" in text


# --- honest empty states (§5.1) --------------------------------------------


@pytest.mark.asyncio
async def test_no_pack_reports_preparing_not_nothing_found(tmp_path, monkeypatch):
    """Two different sentences to a student, and only one of them invites them
    back."""
    import coursemate_service.boundary.impl as impl
    from coursemate_service.api.plan import deterministic_plan

    monkeypatch.setattr(impl, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "empty.db"))
    frames = await _collect(deterministic_plan(ExamPrepRequest(request="revise"), _claims()))
    assert frames[-1].error_code == ErrorCode.PREPARING


@pytest.mark.asyncio
async def test_status_says_why_the_tab_is_empty(tmp_path, monkeypatch):
    import coursemate_service.boundary.impl as impl
    from coursemate_service.api import examprep

    monkeypatch.setattr(impl, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "empty.db"))
    status = await examprep.status(_claims())
    assert status.pack_loaded is False
    assert status.questions == 0


@pytest.mark.asyncio
async def test_status_reports_the_banks_soft_spots(loaded):
    """A student should know the question bank has items the extractor was
    unsure about, rather than reading every one as exact."""
    from coursemate_service.api import examprep

    status = await examprep.status(_claims())
    assert (status.pack_loaded, status.questions, status.clos) == (True, 3, 3)
    assert status.low_confidence == 1
    assert (status.earliest_year, status.latest_year) == (2022, 2024)


# --- pack loading is service-credentialed ----------------------------------


@pytest.mark.asyncio
async def test_loading_another_tenants_pack_is_refused(tmp_path, monkeypatch):
    """Refused, not rewritten. Coercing the field would be a silent cross-tenant
    write, and carrying `tenant` from day one is only worth it if it is checked."""
    from fastapi import HTTPException

    import coursemate_service.api.packs as packs

    monkeypatch.setattr(packs, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "p.db"))
    with pytest.raises(HTTPException) as exc:
        await packs.load_pack(ExamPrepPack(offering_id=OFFERING, tenant="someone-else"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_question_ids_are_named_not_swallowed(tmp_path, monkeypatch):
    """Otherwise the UNIQUE constraint rolls the whole pack back and the operator
    reads an IntegrityError out of a 500."""
    from fastapi import HTTPException

    import coursemate_service.api.packs as packs

    monkeypatch.setattr(packs, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "p.db"))
    with pytest.raises(HTTPException) as exc:
        await packs.load_pack(ExamPrepPack(
            offering_id=OFFERING, tenant=TENANT,
            questions=[_question("DUP", "CLO-1"), _question("DUP", "CLO-2")],
        ))
    assert "DUP" in exc.value.detail


@pytest.mark.asyncio
async def test_loading_reports_unconfirmed_clos(tmp_path, monkeypatch):
    """§7.3: assisted, never asserted. The count is returned so the operator can
    see they are shipping an unconfirmed spine."""
    import coursemate_service.api.packs as packs

    monkeypatch.setattr(packs, "get_examprep_store",
                        lambda: ExamPrepStore(tmp_path / "p.db"))
    out = await packs.load_pack(ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=[CLO(clo_id="CLO-1", text="a"), CLO(clo_id="CLO-2", text="b", confirmed_by="x")],
    ))
    assert out["unconfirmed_clos"] == 1


def test_the_request_contract_has_no_identity_field():
    """Scope comes from the verified JWT. A payload field for it would be a field
    an attacker can set."""
    fields = set(ExamPrepRequest.model_fields)
    assert not (fields & {"student_id", "offering_id", "course_id", "tenant", "sub"})
    assert json.loads(ExamPrepRequest(request="x").model_dump_json())["mastery"] is None
