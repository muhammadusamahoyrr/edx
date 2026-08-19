"""Phase 1C — the practice endpoint, end to end.

One acceptance test walks the whole student journey against real components: the
real `QuizGenerator`, the real `ai.gate`, the real boundary with a real chunk
index and a real exam pack, the real SSE encoder, the real `PracticeQuestion`
contract. Only the model is scripted, because a provider is the one thing a test
must not depend on.

The negative tests matter more. §9.0 lets this output reach a student with **no
instructor approval**, so every path that could put an unlabelled, unsourced or
ungrounded question in front of them has to be pinned.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.examprep import (
    CLO,
    ExamPrepPack,
    ExamType,
    PracticeRequest,
    QuestionRecord,
)
from coursemate_service.knowledge.examprep_store import ExamPrepStore
from coursemate_service.knowledge.store import ChunkStore

TENANT = "default"
OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"

GENERATED = (
    "Two processes each hold one resource and request the other. Explain why "
    "neither can proceed and identify which condition must be broken."
)
OK = json.dumps({"question": GENERATED})


def _claims(offering: str = OFFERING) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="u1", username="alice", course_id=offering, offering_id=offering,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
    )


class _Router:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0

    async def acompletion(self, **kw):
        self.calls += 1
        content = self.payloads.pop(0) if self.payloads else None
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(
            model="stub/model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A real index and a real pack behind the real boundary."""
    chunks = ChunkStore(tmp_path / "index.db")
    chunks.write_chunks([{
        "tenant": TENANT, "course_id": OFFERING, "offering_id": OFFERING,
        "usage_key": "block-v1:deadlock", "block_id": "b1", "block_type": "html",
        "content_type": "text", "display_name": "Deadlock avoidance", "version": "v1",
        "ordinal": 0,
        "text": ("A deadlock arises when processes hold resources and wait on each "
                 "other in a circular chain. Deadlock avoidance uses the banker's "
                 "algorithm to keep the system in a safe state."),
    }])
    chunks.swap(OFFERING, "v1")

    exams = ExamPrepStore(tmp_path / "examprep.db")
    exams.load_pack(ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=[CLO(clo_id="CLO-1", text="Deadlock and concurrency", confirmed_by="dr-lee"),
              CLO(clo_id="CLO-2", text="Scheduling")],
        questions=[QuestionRecord(
            question_id="Q1", tenant=TENANT, offering_id=OFFERING,
            source_doc_id="final-2024.pdf", page=3,
            text="Explain how a deadlock arises between two processes.",
            clo_id="CLO-1", year=2024, marks=10, exam_type=ExamType.FINAL,
            difficulty=0.6,
        )],
    ))

    import coursemate_service.boundary.impl as impl

    monkeypatch.setattr(impl, "get_store", lambda: chunks)
    monkeypatch.setattr(impl, "get_examprep_store", lambda: exams)
    return SimpleNamespace(monkeypatch=monkeypatch)


def _with_router(stack, router):
    from coursemate_service.ai import quiz_generator as qg

    stack.monkeypatch.setattr(qg, "get_router", lambda: router)


async def _post(request, claims):
    """Drive the real route and decode the SSE body into frames."""
    from coursemate_service.api import examprep

    response = await examprep.practice_stream(request, claims)
    body = "".join([chunk async for chunk in response.body_iterator])
    frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    return response, body, frames


# --- THE ACCEPTANCE TEST ---------------------------------------------------


@pytest.mark.asyncio
async def test_student_selects_clo_and_difficulty_and_receives_a_labelled_question(stack):
    """Student -> Exam Prep -> select CLO + difficulty -> source question found
    -> lesson context retrieved -> gate passes -> question generated -> SSE
    streamed -> AI badge and provenance renderable.

    One test, the whole flow, real components throughout.
    """
    from coursemate_service.api import examprep

    router = _Router(OK)
    _with_router(stack, router)
    claims = _claims()

    # 1. the tab loads and offers the selector
    status = await examprep.status(claims)
    assert status.pack_loaded is True
    assert [(c.clo_id, c.confirmed) for c in status.clo_options] == [
        ("CLO-1", True), ("CLO-2", False)
    ]

    # 2. the student picks an outcome and a level
    response, body, frames = await _post(
        PracticeRequest(clo_id="CLO-1", difficulty_band="medium"), claims
    )

    # 3. it streamed as SSE, unbuffered
    assert response.media_type == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert body.startswith("data: ") and body.endswith("\n\n")

    # 4. the model was consulted exactly once
    assert router.calls == 1

    # 5. the question arrived
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "token" and kinds[-1] == "done"
    text = "".join(f.get("text", "") for f in frames if f["type"] == "token")
    assert text == GENERATED

    # 6. provenance the UI renders: the source paper first, then the lesson
    cited = [f["citation"]["usage_key"] for f in frames if f["type"] == "citation"]
    assert cited == ["final-2024.pdf", "block-v1:deadlock"]

    # 7. and the badge's claim is true of the object, not just the CSS
    from coursemate_service.ai.quiz_generator import QuizGenerator

    built = QuizGenerator()._build(text, _source_of(stack), ["block-v1:deadlock"])
    assert built.ai_generated is True
    assert built.derived_from == ["Q1", "block-v1:deadlock"]


def _source_of(stack):
    from coursemate_service.boundary.impl import boundary

    return boundary.search_past_questions(OFFERING, _claims(), clo_id="CLO-1")[0]


# --- negative cases --------------------------------------------------------


@pytest.mark.asyncio
async def test_no_source_question_abstains(stack):
    """A CLO with no past-paper question generates nothing. The safety property,
    and it fires before any model call."""
    router = _Router(OK)
    _with_router(stack, router)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-2"), _claims())
    assert frames[-1]["error_code"] == "abstained"
    assert router.calls == 0


@pytest.mark.asyncio
async def test_unavailable_context_reports_preparing(stack, monkeypatch):
    """An unindexed course is "still being prepared", never "not covered" — two
    different sentences to a student (§5.1)."""
    import coursemate_service.boundary.impl as impl

    _with_router(stack, _Router(OK))
    monkeypatch.setattr(impl.CourseIntelligenceImpl, "has_index", lambda self, o: False)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert frames[-1]["error_code"] == "preparing"


@pytest.mark.asyncio
async def test_gate_rejection_abstains(stack, monkeypatch):
    """The real gate, at a threshold nothing can reach. No question is written
    over material that failed the confidence bar."""
    from coursemate_service.ai import gate

    router = _Router(OK)
    _with_router(stack, router)
    monkeypatch.setattr(gate.settings, "confidence_threshold", 0.99)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert frames[-1]["error_code"] == "abstained"
    assert router.calls == 0


@pytest.mark.asyncio
async def test_provider_failure_reports_unavailable(stack):
    """A dead provider is reported honestly and never fabricated around."""
    _with_router(stack, _Router(RuntimeError("provider down")))

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert frames[-1]["error_code"] == "unavailable"
    assert not [f for f in frames if f["type"] == "token"]


@pytest.mark.asyncio
async def test_malformed_generation_retries_once_then_abstains(stack):
    """Two unusable outputs and the student sees nothing — never a question the
    contract rejected."""
    router = _Router("not json", '{"wrong_key": 1}')
    _with_router(stack, router)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert router.calls == 2
    assert frames[-1]["error_code"] == "abstained"
    assert [f["type"] for f in frames] == ["error"]


@pytest.mark.asyncio
async def test_malformed_then_valid_still_succeeds(stack):
    router = _Router("not json", OK)
    _with_router(stack, router)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert router.calls == 2
    assert frames[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_a_token_for_another_offering_gets_nothing(stack):
    """Unauthorized access. The token scopes the request; the generator reads its
    own offering, which for this caller holds no pack — so it abstains rather
    than serving another cohort's papers."""
    router = _Router(OK)
    _with_router(stack, router)

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims(OTHER))
    assert frames[-1]["error_code"] == "abstained"
    assert router.calls == 0


@pytest.mark.asyncio
async def test_enrollment_is_re_derived_and_failure_denies(stack, monkeypatch):
    """§10.1: a signed token is not a grant. With enforcement on and the platform
    unreachable, the boundary fails closed and nothing is generated."""
    import coursemate_service.boundary.impl as impl

    router = _Router(OK)
    _with_router(stack, router)
    monkeypatch.setattr(impl.settings, "enforce_enrollment", True)
    monkeypatch.setattr(
        impl.verifier, "require_enrolled",
        lambda *a, **k: (_ for _ in ()).throw(impl.PlatformUnreachable("lms down")),
    )

    _, _, frames = await _post(PracticeRequest(clo_id="CLO-1"), _claims())
    assert frames[-1]["error_code"] == "abstained"
    assert router.calls == 0


# --- the wire contract -----------------------------------------------------


def test_the_request_carries_no_identity():
    """Scope comes from the JWT. A payload field for it would be a field an
    attacker can set, and the source question is chosen server-side.

    **`mastery` was added 2026-08-19 and this test was widened deliberately.**
    It is not an identity field, but `MasterySnapshot` carries an `offering_id`
    of its own — so the payload now *does* contain an attacker-settable offering
    id, nested. The guarantee therefore moved from "absent" to "checked", which
    is the same position `StudyPlanRequest` has held since it shipped.

    Widening the allow-list without asserting the new guarantee would have
    turned this test into a rubber stamp, so the check that actually matters is
    pinned below and in
    `test_quiz_generator.py::test_a_snapshot_from_another_offering_is_ignored`.
    """
    fields = set(PracticeRequest.model_fields)
    assert fields == {"clo_id", "difficulty_band", "mastery"}
    # Still no identity at the TOP level: nothing here names the student, the
    # offering, the tenant, or the source question. `offering_id` stays in this
    # set deliberately — it appears only nested inside `mastery`, where it is
    # discarded on mismatch, and a future top-level one would be a real widening
    # that this line is here to catch.
    assert not (fields & {"student_id", "offering_id", "tenant", "question_id"})


def test_a_nested_offering_id_cannot_widen_scope():
    """The price of carrying `mastery`: the payload gained a settable
    `offering_id` one level down. It must shape nothing.

    Asserted against the generator's own guard rather than end-to-end, because
    this is the line that decides it — a snapshot minted elsewhere is discarded,
    leaving the seed order exactly as it would have been with no snapshot.
    """
    from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
    from coursemate_service.ai.quiz_generator import QuizGenerator

    forged = MasterySnapshot(
        offering_id="course-v1:Someone+Else+2024",
        clos=[CLOMastery(clo_id="CLO-1", attempts=7, correct=0)],
    )
    assert QuizGenerator._rotation_index(forged, OFFERING, "CLO-1", 3) == 0


@pytest.mark.parametrize("band", ["trivial", "EASY", "0", "hardest"])
def test_an_invalid_difficulty_band_is_rejected_at_the_wire(band):
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PracticeRequest(clo_id="CLO-1", difficulty_band=band)


def test_an_absent_band_is_not_defaulted():
    """"Any level" is not "easy". Guessing a preference would silently narrow
    what the student is offered."""
    assert PracticeRequest(clo_id="CLO-1").difficulty_band is None


@pytest.mark.asyncio
async def test_status_offers_no_outcomes_when_the_caller_is_denied(stack, monkeypatch):
    """An empty selector is the honest render for an unconfirmable enrollment —
    never another cohort's outcomes."""
    import coursemate_service.boundary.impl as impl
    from coursemate_service.api import examprep

    monkeypatch.setattr(
        impl.CourseIntelligenceImpl, "get_clos",
        lambda self, o, c: (_ for _ in ()).throw(impl.AuthorizationError("denied")),
    )
    status = await examprep.status(_claims())
    assert status.clo_options == []


# --- the mastery snapshot reaches the generator (seed rotation) --------------


def test_practice_request_still_validates_without_mastery():
    """The compatibility guarantee, at the contract boundary. A browser that
    predates the field sends exactly this and must not get a 422."""
    request = PracticeRequest(clo_id="CLO-1", difficulty_band="medium")
    assert request.mastery is None


def test_practice_request_accepts_the_same_snapshot_study_plan_takes():
    """One representation, not two. `StudyPlanRequest.mastery` and this are the
    same type, so the browser posts the object it already holds."""
    from coursemate_contracts.examprep import StudyPlanRequest
    from coursemate_contracts.mastery import CLOMastery, MasterySnapshot

    snap = MasterySnapshot(
        offering_id=OFFERING,
        clos=[CLOMastery(clo_id="CLO-1", attempts=2, correct=1)],
    )
    practice = PracticeRequest(clo_id="CLO-1", mastery=snap)
    plan = StudyPlanRequest(marks_budget=50, mastery=snap)

    assert practice.mastery == plan.mastery
    assert (
        PracticeRequest.model_fields["mastery"].annotation
        == StudyPlanRequest.model_fields["mastery"].annotation
    )


@pytest.mark.asyncio
async def test_the_route_forwards_mastery_to_the_generator(stack, monkeypatch):
    """A field the contract accepts and the route drops is a field that does
    nothing — this repo has shipped that shape before. Assert it arrives."""
    from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
    seen = {}

    async def _spy(claims, **kw):
        seen.update(kw)
        if False:
            yield  # pragma: no cover - makes this an async generator

    from coursemate_service.ai import quiz_generator as qg
    monkeypatch.setattr(qg.generator, "stream", _spy)
    snap = MasterySnapshot(
        offering_id=OFFERING,
        clos=[CLOMastery(clo_id="CLO-1", attempts=3, correct=1)],
    )
    await _post(PracticeRequest(clo_id="CLO-1", mastery=snap), _claims())

    assert seen.get("mastery") is snap, "the route dropped the snapshot"
    assert seen.get("clo_id") == "CLO-1"
