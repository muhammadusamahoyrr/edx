"""The practice-question generator — a two-stage pipeline, not an agent.

§9.0 lets a generated question reach a student with **no instructor gate**,
because it is labelled, cited and measured. Every test here defends one of those
three: the label and the citation must be impossible for the model to influence,
and anything unmeasurable must abstain rather than ship.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import Citation, FrameType
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.mastery import CLOMastery, MasterySnapshot
from coursemate_contracts.examprep import (
    ExamType,
    PracticeQuestion,
    QuestionRecord,
    band_of,
    derive_difficulty,
)
from coursemate_service.ai.context import ContextChunk, ContextResult
from coursemate_service.ai.quiz_generator import QuizGenerator

OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"


def _claims() -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="u1", username="alice", course_id=OFFERING, offering_id=OFFERING,
        exp=now + 300, iat=now,
    )


def _source(**kw) -> QuestionRecord:
    base = dict(
        question_id="Q1", tenant="default", offering_id=OFFERING,
        source_doc_id="final-2024.pdf", page=3,
        text="Explain how a deadlock arises between two processes.",
        clo_id="CLO-1", year=2024, marks=10, exam_type=ExamType.FINAL, difficulty=0.6,
    )
    return QuestionRecord(**{**base, **kw})


def _grounded(usage_key: str = "block-v1:lesson") -> ContextResult:
    return ContextResult(
        chunks=[ContextChunk(
            text="A deadlock arises when processes wait on each other in a circular chain.",
            citation=Citation(usage_key=usage_key, display_name="Deadlock avoidance"),
            score=0.9,
        )],
        top_score=0.9,
    )


class _Ctx:
    """Stand-in ContextProvider. The gate is the real one."""

    def __init__(self, result: ContextResult):
        self.result = result

    def fetch_sync(self, question, claims, limit=None):
        return self.result


class _Router:
    """Returns a scripted sequence of raw model outputs."""

    def __init__(self, *payloads: str | None):
        self.payloads = list(payloads)
        self.calls = 0

    async def acompletion(self, **kw):
        self.calls += 1
        content = self.payloads.pop(0) if self.payloads else None
        return SimpleNamespace(
            model="stub/model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def _make(monkeypatch, *, source, context, router):
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg, "get_router", lambda: router)
    monkeypatch.setattr(qg.boundary, "search_past_questions",
                        lambda *a, **k: ([source] if source else []))
    return QuizGenerator(context_provider=_Ctx(context))


async def _run(gen, **kw):
    return [f async for f in gen.stream(_claims(), clo_id="CLO-1", **kw)]


def _text(frames) -> str:
    return "".join(f.text or "" for f in frames if f.type == FrameType.TOKEN)


OK = '{"question": "Given two processes holding one resource each, explain why neither can proceed."}'


# --- the happy path: definition of done ------------------------------------


@pytest.mark.asyncio
async def test_a_real_source_produces_a_schema_valid_practice_question(monkeypatch):
    """The whole two-stage pipeline: real source record -> gated lesson context
    -> one model call -> validated `PracticeQuestion` -> frames."""
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))
    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE
    assert "neither can proceed" in _text(frames)

    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]
    assert cited == ["final-2024.pdf", "block-v1:lesson"]


@pytest.mark.asyncio
async def test_the_object_it_builds_satisfies_the_contract(monkeypatch):
    """Built directly, so the assertion is about the object rather than the
    rendered text — this is what the Feature B rubric will score."""
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))
    q = gen._build("A new question.", _source(), ["block-v1:lesson"])

    assert isinstance(q, PracticeQuestion)
    assert q.ai_generated is True
    assert q.derived_from == ["Q1", "block-v1:lesson"]
    assert (q.marks, q.difficulty, q.clo_id) == (10, 0.6, "CLO-1")
    assert q.difficulty_is_derived is True
    assert band_of(q.difficulty) == "medium"


# --- stage 1: no source, no question ---------------------------------------


@pytest.mark.asyncio
async def test_no_source_question_abstains_before_any_model_call(monkeypatch):
    """The safety property, and it is free: it fires before generation, so
    refusing costs nothing."""
    router = _Router(OK)
    gen = _make(monkeypatch, source=None, context=_grounded(), router=router)
    frames = await _run(gen)

    assert frames[-1].error_code == ErrorCode.ABSTAINED
    assert router.calls == 0


@pytest.mark.asyncio
async def test_a_denied_source_lookup_abstains_rather_than_erroring(monkeypatch):
    """Denied scope returns nothing, never another cohort's papers — and the
    student sees 'not covered', not an error confirming the content exists."""
    from coursemate_service.ai import quiz_generator as qg
    from coursemate_service.boundary.impl import AuthorizationError

    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))

    def denied(*a, **k):
        raise AuthorizationError("token scoped elsewhere")

    monkeypatch.setattr(qg.boundary, "search_past_questions", denied)
    assert (await _run(gen))[-1].error_code == ErrorCode.ABSTAINED


# --- stage 2: the gate decides, using the real gate ------------------------


@pytest.mark.asyncio
async def test_weak_context_abstains(monkeypatch):
    """Below the confidence bar there is nothing to ground a question in. The
    real `ai.gate` decides, at the configured threshold."""
    weak = ContextResult(chunks=[], top_score=0.0, index_missing=False)
    router = _Router(OK)
    gen = _make(monkeypatch, source=_source(), context=weak, router=router)

    assert (await _run(gen))[-1].error_code == ErrorCode.ABSTAINED
    assert router.calls == 0


@pytest.mark.asyncio
async def test_an_unindexed_course_reports_preparing_not_abstained(monkeypatch):
    """§5.1: two different sentences, and only one invites the student back."""
    missing = ContextResult(chunks=[], top_score=0.0, index_missing=True)
    gen = _make(monkeypatch, source=_source(), context=missing, router=_Router(OK))

    assert (await _run(gen))[-1].error_code == ErrorCode.PREPARING


class _CtxByText:
    """Stand-in ContextProvider that scores each SOURCE QUESTION differently.

    `_Ctx` returns one result whatever it is asked, which is why the gate looked
    like a property of the outcome. It is a property of the individual seed
    question, and telling those apart needs a double that can disagree with
    itself.
    """

    def __init__(self, by_text: dict[str, ContextResult]):
        self.by_text = by_text
        self.asked: list[str] = []

    def fetch_sync(self, question, claims, limit=None):
        self.asked.append(question)
        return self.by_text[question]


def _scored(top_score: float) -> ContextResult:
    """Grounded context at an exact score, to sit either side of tau."""
    return ContextResult(
        chunks=[ContextChunk(
            text="A deadlock arises when processes wait on each other in a circular chain.",
            citation=Citation(usage_key="block-v1:lesson", display_name="Deadlock avoidance"),
            score=top_score,
        )],
        top_score=top_score,
    )


def _make_multi(monkeypatch, *, sources, ctx, router):
    """`_make`, but with several candidates for the outcome."""
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg, "get_router", lambda: router)
    monkeypatch.setattr(qg.boundary, "search_past_questions", lambda *a, **k: list(sources))
    return QuizGenerator(context_provider=ctx)


@pytest.mark.asyncio
async def test_a_weak_first_candidate_falls_through_to_a_usable_one(monkeypatch):
    """The bug this exists for, in the shape it was found in.

    OEX101 CLO-1: the store orders `year DESC, marks DESC`, so the 15-mark
    "critically evaluate ... governance" essay leads. Its own text scored 0.3458
    against tau=0.35 — short by 0.004 — while "Name two major members of the Open
    edX community" scored 0.8500. The generator gated the first candidate and
    abstained, telling the student the best-covered outcome in the course was not
    covered, with a usable seed already sitting in `candidates`.

    The gate judges the SEED, not the outcome. One weak seed must not condemn the
    outcome while other seeds exist.
    """
    essay = _source(question_id="Q-ESSAY", marks=15,
                    text="Critically evaluate the claim that the community's governance is its main strength")
    naming = _source(question_id="Q-NAME", marks=3,
                     text="Name two major members of the Open edX community")
    ctx = _CtxByText({essay.text: _scored(0.3458), naming.text: _scored(0.8500)})
    router = _Router(OK)
    gen = _make_multi(monkeypatch, sources=[essay, naming], ctx=ctx, router=router)

    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE, "generation did not proceed"
    assert router.calls == 1, "the model was never asked"
    # Both were gated, in order, and the passing one is the one it used.
    assert ctx.asked == [essay.text, naming.text]
    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]
    assert "final-2024.pdf" in cited


@pytest.mark.asyncio
async def test_the_second_candidate_becomes_the_source_it_was_modelled_on(monkeypatch):
    """Falling through must move the PROVENANCE too.

    Reporting the essay as the source while modelling on the naming question
    would be a false claim about where the question came from — the same class of
    error as labelling a reprint AI-generated.
    """
    essay = _source(question_id="Q-ESSAY", marks=15, difficulty=0.9,
                    text="Critically evaluate the claim that the community's governance is its main strength")
    naming = _source(question_id="Q-NAME", marks=3, difficulty=0.2,
                     source_doc_id="paper-B.pdf",
                     text="Name two major members of the Open edX community")
    ctx = _CtxByText({essay.text: _scored(0.10), naming.text: _scored(0.90)})
    gen = _make_multi(monkeypatch, sources=[essay, naming], ctx=ctx, router=_Router(OK))

    frames = await _run(gen)

    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]
    assert "paper-B.pdf" in cited, "cited the seed it did not use"
    assert "final-2024.pdf" not in cited


@pytest.mark.asyncio
async def test_every_candidate_below_the_bar_still_abstains(monkeypatch):
    """The safety property is unchanged: no usable seed means no question.

    Falling through must not become "keep looking until something passes" — if
    nothing reaches tau, the generator still refuses, and still before any model
    call.
    """
    a = _source(question_id="Q-A", text="Alpha question text")
    b = _source(question_id="Q-B", text="Beta question text")
    c = _source(question_id="Q-C", text="Gamma question text")
    ctx = _CtxByText({a.text: _scored(0.10), b.text: _scored(0.20), c.text: _scored(0.3499)})
    router = _Router(OK)
    gen = _make_multi(monkeypatch, sources=[a, b, c], ctx=ctx, router=router)

    frames = await _run(gen)

    assert frames[-1].error_code == ErrorCode.ABSTAINED
    assert router.calls == 0, "abstention must cost no model call"
    assert ctx.asked == [a.text, b.text, c.text], "it stopped looking early"


@pytest.mark.asyncio
async def test_an_unindexed_course_still_reports_preparing_across_candidates(monkeypatch):
    """§5.1 survives the loop. A missing index is a property of the index, so
    every candidate reports it — and the student must still be invited back
    rather than told the material does not exist."""
    a = _source(question_id="Q-A", text="Alpha question text")
    b = _source(question_id="Q-B", text="Beta question text")
    missing = ContextResult(chunks=[], top_score=0.0, index_missing=True)
    ctx = _CtxByText({a.text: missing, b.text: missing})
    gen = _make_multi(monkeypatch, sources=[a, b], ctx=ctx, router=_Router(OK))

    assert (await _run(gen))[-1].error_code == ErrorCode.PREPARING


@pytest.mark.asyncio
async def test_a_passing_first_candidate_is_not_second_guessed(monkeypatch):
    """The common path must be untouched: one candidate passes, one retrieval
    happens, and no later candidate is even scored."""
    good = _source(question_id="Q-GOOD", text="Alpha question text")
    other = _source(question_id="Q-OTHER", text="Beta question text")
    ctx = _CtxByText({good.text: _scored(0.90), other.text: _scored(0.99)})
    gen = _make_multi(monkeypatch, sources=[good, other], ctx=ctx, router=_Router(OK))

    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE
    assert ctx.asked == [good.text], "it kept scoring after a candidate passed"


@pytest.mark.asyncio
async def test_the_reprint_check_still_sees_every_candidate(monkeypatch):
    """Falling through must not narrow the duplicate check.

    `_is_reprint` compares against ALL candidates, not the chosen seed. If the
    model reproduces the FIRST candidate — the one the gate rejected — that is
    still a real exam question labelled AI-generated, and it must still be
    caught.
    """
    essay = _source(question_id="Q-ESSAY",
                    text="Critically evaluate the claim that governance is the main strength")
    naming = _source(question_id="Q-NAME", text="Name two major members of the Open edX community")
    ctx = _CtxByText({essay.text: _scored(0.10), naming.text: _scored(0.90)})
    # The model parrots the REJECTED candidate back; both attempts do.
    reprint = '{"question": "Critically evaluate the claim that governance is the main strength"}'
    router = _Router(reprint, reprint)
    gen = _make_multi(monkeypatch, sources=[essay, naming], ctx=ctx, router=router)

    frames = await _run(gen)

    assert frames[-1].error_code == ErrorCode.ABSTAINED, "a reprint reached the student"
    assert router.calls == 2, "the reprint did not spend its retry"


# --- the semantic duplicate band (P0-calibrated 0.86 / 0.92) ----------------
#
# Token overlap catches a copy and is blind to a rewording; cosine catches a
# rewording and, on short factual questions, confuses topic with identity. The
# two run together because they fail differently. These tests drive the cosine
# side with a stubbed provider — the thresholds are the decision under test, the
# transport is not.


def _fake_embedding(score_for_first_candidate: float):
    """An `aembedding` stand-in producing an exact cosine against candidate 0.

    Two dimensions are enough: put the generated question on the unit x-axis and
    rotate each candidate to the angle whose cosine is the score wanted. No
    provider, no network, and the number under test is exact rather than
    approximately whatever an embedding model happens to return.
    """
    import math as _math

    async def _aembedding(model, input, **kw):  # noqa: A002 - litellm's kwarg name
        vectors = [[1.0, 0.0]]
        for i in range(len(input) - 1):
            s = score_for_first_candidate if i == 0 else 0.10
            vectors.append([s, _math.sqrt(max(0.0, 1.0 - s * s))])
        return SimpleNamespace(data=[{"embedding": v} for v in vectors])

    return _aembedding


def _enable_semantic(monkeypatch, cosine: float):
    """Turn the check on and pin what the provider will report."""
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg.settings, "duplicate_embedding_model", "stub/embed")
    fake = SimpleNamespace(aembedding=_fake_embedding(cosine))
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake)


@pytest.mark.asyncio
async def test_a_semantic_duplicate_at_or_above_the_bar_is_rejected(monkeypatch):
    """0.92+. A reworded past-paper question labelled AI-generated is the same
    false claim to the student as a copied one — §9.0 rests on that label."""
    _enable_semantic(monkeypatch, 0.95)
    router = _Router(OK, OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)

    assert frames[-1].error_code == ErrorCode.ABSTAINED, "a paraphrase reached the student"
    assert router.calls == 2, "rejection did not spend the retry"


@pytest.mark.asyncio
async def test_the_uncertain_band_retries_rather_than_refusing(monkeypatch):
    """0.86–0.92 is the measured OVERLAP: a genuinely different question was
    observed at 0.8850. Spend a retry, but if that was the last attempt serve
    the question — refusing outright would deny legitimate questions on evidence
    that cannot support it."""
    _enable_semantic(monkeypatch, 0.8850)
    router = _Router(OK, OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE, "an uncertain score refused permanently"
    assert router.calls == 2, "the uncertain band did not spend a retry"
    assert _text(frames), "nothing was served"


@pytest.mark.asyncio
async def test_a_hard_negative_at_0885_is_not_automatically_rejected(monkeypatch):
    """The specific pair from the calibration — 'State what a named release is'
    against 'Give one example of a named release'. Different questions; a
    student can answer either without the other."""
    _enable_semantic(monkeypatch, 0.8850)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK, OK))

    assert (await _run(gen))[-1].type == FrameType.DONE


@pytest.mark.asyncio
async def test_below_the_band_is_accepted_without_a_retry(monkeypatch):
    """Real accepted output measured 0.6792–0.7928 against its own seed. That
    must cost nothing — no retry, no second model call."""
    _enable_semantic(monkeypatch, 0.7928)
    router = _Router(OK, OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE
    assert router.calls == 1, "an accepted question was retried anyway"


@pytest.mark.asyncio
async def test_an_embedding_failure_falls_back_to_token_overlap(monkeypatch):
    """A duplicate check that cannot run must not take generation down with it.
    `None` means 'no opinion', never 'not a duplicate' — Jaccard is still the
    floor, so the guarantee degrades rather than disappearing."""
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg.settings, "duplicate_embedding_model", "stub/embed")

    async def _boom(model, input, **kw):  # noqa: A002
        raise RuntimeError("provider down")

    monkeypatch.setitem(__import__("sys").modules, "litellm",
                        SimpleNamespace(aembedding=_boom))
    router = _Router(OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)

    assert frames[-1].type == FrameType.DONE, "a provider outage broke generation"
    assert router.calls == 1


@pytest.mark.asyncio
async def test_a_reprint_is_still_caught_when_embeddings_are_unavailable(monkeypatch):
    """The floor holds. Token overlap is not weakened by the second check being
    absent, which is the whole reason it was kept."""
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg.settings, "duplicate_embedding_model", "stub/embed")

    async def _boom(model, input, **kw):  # noqa: A002
        raise RuntimeError("provider down")

    monkeypatch.setitem(__import__("sys").modules, "litellm",
                        SimpleNamespace(aembedding=_boom))
    src = _source(text="Explain how a deadlock arises between two processes.")
    parroted = '{"question": "Explain how a deadlock arises between two processes."}'
    gen = _make(monkeypatch, source=src, context=_grounded(),
                router=_Router(parroted, parroted))

    assert (await _run(gen))[-1].error_code == ErrorCode.ABSTAINED


@pytest.mark.asyncio
async def test_the_check_is_skipped_when_no_embedding_model_is_configured(monkeypatch):
    """The default in tests, and the correct behaviour for a deployment without
    an embedding provider. It must cost nothing, not fail."""
    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg.settings, "duplicate_embedding_model", "")
    called = False

    async def _tripwire(model, input, **kw):  # noqa: A002
        nonlocal called
        called = True
        raise AssertionError("the provider was called with no model configured")

    monkeypatch.setitem(__import__("sys").modules, "litellm",
                        SimpleNamespace(aembedding=_tripwire))
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))

    assert (await _run(gen))[-1].type == FrameType.DONE
    assert called is False


@pytest.mark.asyncio
async def test_the_embedding_call_uses_its_own_timeout_not_the_model_one(monkeypatch):
    """The duplicate check must not borrow the generation ceiling.

    `model_timeout_seconds` is 300 in the live deployment. A ~500 ms check
    bounded by it would hold a student's connection for five minutes per
    attempt against a reachable-but-wedged provider — the `socat` forwarder
    listening while Ollama is unusable is a documented failure of this stack.

    Driven by behaviour rather than by inspecting the call: the model ceiling is
    set absurdly high, the dedicated one absurdly low, and the provider hangs.
    If the wrong setting were used the test would take 300 s.
    """
    import asyncio as _asyncio
    import time as _time

    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg.settings, "duplicate_embedding_model", "stub/embed")
    monkeypatch.setattr(qg.settings, "model_timeout_seconds", 300)
    monkeypatch.setattr(qg.settings, "semantic_embedding_timeout_seconds", 0.05)

    async def _hangs(model, input, **kw):  # noqa: A002
        await _asyncio.sleep(30)

    monkeypatch.setitem(__import__("sys").modules, "litellm",
                        SimpleNamespace(aembedding=_hangs))
    router = _Router(OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    started = _time.monotonic()
    frames = await _run(gen)
    elapsed = _time.monotonic() - started

    # Gave up on the dedicated bound, then served on the token-overlap check.
    assert frames[-1].type == FrameType.DONE, "a hung provider broke generation"
    assert router.calls == 1
    assert elapsed < 5.0, (
        f"took {elapsed:.2f}s — the embedding call is bounded by the wrong "
        "setting, or is not bounded at all"
    )


def test_the_embedding_timeout_is_a_setting_of_its_own():
    """A separate name, so raising the generation ceiling cannot silently raise
    this one. 5 s is ~8x the 490-623 ms measured for a batch of four."""
    from coursemate_service.config import Settings

    field = Settings.model_fields["semantic_embedding_timeout_seconds"]
    assert field.default == 5.0
    # Two distinct fields, so raising one cannot raise the other.
    assert {"semantic_embedding_timeout_seconds", "model_timeout_seconds"} <= set(
        Settings.model_fields
    )
    assert Settings.model_fields["model_timeout_seconds"].default == 60


# --- seed rotation: a student must not see one seed forever -----------------
#
# The generator models each question on a real past-paper one, and the candidate
# list is ordered `year DESC, marks DESC` — so the same outcome led with the same
# seed on every request. Rotating by the student's own attempt count spreads the
# questions across an outcome's seeds without any server-side state.


def _snapshot(offering: str, clo_id: str, attempts: int) -> MasterySnapshot:
    return MasterySnapshot(
        offering_id=offering,
        clos=[CLOMastery(clo_id=clo_id, attempts=attempts, correct=0)],
    )


def _seeds():
    """Three candidates, in the order the store returns them."""
    # Distinct `source_doc_id` per seed: the paper citation names it, which is
    # how a test can see WHICH seed the generator modelled on.
    return [
        _source(question_id="Q-A", source_doc_id="paper-A.pdf", text="Alpha question text"),
        _source(question_id="Q-B", source_doc_id="paper-B.pdf", text="Beta question text"),
        _source(question_id="Q-C", source_doc_id="paper-C.pdf", text="Gamma question text"),
    ]


def _ctx_all_pass():
    return _CtxByText({s.text: _scored(0.90) for s in _seeds()})


async def _seed_used(monkeypatch, *, attempts=None, offering=OFFERING, band=None):
    """Which seed the generator actually modelled on, via `derived_from`."""
    seeds = _seeds()
    gen = _make_multi(monkeypatch, sources=seeds, ctx=_ctx_all_pass(), router=_Router(OK))
    snap = None if attempts is None else _snapshot(offering, "CLO-1", attempts)
    frames = [f async for f in gen.stream(
        _claims(), clo_id="CLO-1", difficulty_band=band, mastery=snap)]
    assert frames[-1].type == FrameType.DONE
    # The paper citation is emitted first and names the seed's source document;
    # the seed itself is what `_build` records, so read it from the citation set.
    return next(f.citation.usage_key for f in frames if f.type == FrameType.CITATION)


@pytest.mark.asyncio
async def test_no_mastery_keeps_todays_seed(monkeypatch):
    """The compatibility guarantee. An older browser sends no snapshot, and must
    get exactly the behaviour it got before this existed."""
    seeds = _seeds()
    gen = _make_multi(monkeypatch, sources=seeds, ctx=_ctx_all_pass(), router=_Router(OK))
    frames = [f async for f in gen.stream(_claims(), clo_id="CLO-1")]

    assert frames[-1].type == FrameType.DONE
    assert QuizGenerator._rotation_index(None, OFFERING, "CLO-1", 3) == 0


@pytest.mark.asyncio
async def test_the_seed_advances_as_the_student_practises(monkeypatch):
    """The point of the change: three attempts, three different seeds."""
    used = [await _seed_used(monkeypatch, attempts=n) for n in (0, 1, 2)]

    assert len(set(used)) == 3, f"seeds did not rotate: {used}"


@pytest.mark.asyncio
async def test_rotation_wraps_and_is_deterministic(monkeypatch):
    """Same input, same seed — twice. A student refreshing does not reroll, and
    attempt 3 returns to the first seed rather than running out."""
    assert await _seed_used(monkeypatch, attempts=3) == await _seed_used(monkeypatch, attempts=0)
    assert await _seed_used(monkeypatch, attempts=1) == await _seed_used(monkeypatch, attempts=1)


@pytest.mark.asyncio
async def test_an_explicit_band_is_not_rotated_past(monkeypatch):
    """A stated preference outranks variety. `_find_source` already put the
    band-matching candidate first; rotating would answer a different request."""
    hard = await _seed_used(monkeypatch, attempts=2, band="hard")
    none = await _seed_used(monkeypatch, attempts=0)
    assert hard == none, "an explicit difficulty_band was rotated past"


def test_a_snapshot_from_another_offering_is_ignored():
    """Browser-carried, therefore attacker-controlled — the same rule
    `ai/planner.py` applies. Checked, not trusted."""
    foreign = _snapshot("course-v1:Other+Course+2024", "CLO-1", attempts=2)
    assert QuizGenerator._rotation_index(foreign, OFFERING, "CLO-1", 3) == 0


def test_rotation_falls_back_to_zero_on_every_uncertainty():
    """Zero is today's behaviour, so every unknown degrades to it rather than to
    something arbitrary."""
    snap = _snapshot(OFFERING, "CLO-1", attempts=2)
    assert QuizGenerator._rotation_index(None, OFFERING, "CLO-1", 3) == 0     # no snapshot
    assert QuizGenerator._rotation_index(snap, OFFERING, "CLO-1", 1) == 0     # one candidate
    assert QuizGenerator._rotation_index(snap, OFFERING, "CLO-9", 3) == 0     # unknown outcome
    assert QuizGenerator._rotation_index(snap, OFFERING, "CLO-1", 3) == 2     # and the real case


def test_rotation_sums_attempts_across_bands():
    """`by_clo()` aggregates, so a student who practised easy and hard has moved
    the index by both — not by whichever row happens to come first."""
    snap = MasterySnapshot(offering_id=OFFERING, clos=[
        CLOMastery(clo_id="CLO-1", difficulty_band="easy", attempts=1, correct=1),
        CLOMastery(clo_id="CLO-1", difficulty_band="hard", attempts=1, correct=0),
    ])
    assert QuizGenerator._rotation_index(snap, OFFERING, "CLO-1", 3) == 2


def test_cosine_is_exact_on_known_vectors():
    """The thresholds are the decision, so the arithmetic under them is pinned
    separately from any provider."""
    from coursemate_service.ai.quiz_generator import QuizGenerator as QG

    assert QG._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert QG._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert QG._cosine([1.0, 0.0], [0.92, 0.3919], ) == pytest.approx(0.92, abs=1e-3)
    # A zero vector has no direction; 0.0 beats a ZeroDivisionError on the
    # serve path.
    assert QG._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


@pytest.mark.asyncio
async def test_the_gate_uses_the_configured_threshold(monkeypatch):
    """Not a private bar. A generator that gated differently from chat would
    abstain on cases nobody tested, and both paths would look right alone."""
    from coursemate_service.ai import gate

    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK, OK))
    monkeypatch.setattr(gate.settings, "confidence_threshold", 0.99)
    assert (await _run(gen))[-1].error_code == ErrorCode.ABSTAINED

    monkeypatch.setattr(gate.settings, "confidence_threshold", 0.0)
    assert (await _run(gen))[-1].type == FrameType.DONE


# --- provenance cannot be invented -----------------------------------------


@pytest.mark.asyncio
async def test_the_model_cannot_forge_provenance_or_the_label(monkeypatch):
    """The model returns extra keys claiming a different source and denying it
    is AI-generated. All of it is discarded: only `question` is read, and every
    other field comes from the record the pipeline actually retrieved."""
    forged = (
        '{"question": "A new question about circular waits.",'
        ' "ai_generated": false,'
        ' "derived_from": ["someone-elses-paper.pdf"],'
        ' "clo_id": "CLO-9", "marks": 999, "difficulty": 0.01}'
    )
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(forged))
    q = gen._build(gen._parse(forged), _source(), ["block-v1:lesson"])

    assert q.ai_generated is True
    assert q.derived_from == ["Q1", "block-v1:lesson"]
    assert "someone-elses-paper.pdf" not in q.derived_from
    assert (q.clo_id, q.marks, q.difficulty) == ("CLO-1", 10, 0.6)


@pytest.mark.asyncio
async def test_ai_generated_is_set_by_code_on_every_path(monkeypatch):
    """§9.0's no-instructor-gate argument rests on the label. It is not a default
    the model can override and not a prompt instruction it can ignore."""
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))
    for record in (_source(), _source(marks=None, difficulty=None, clo_id=None)):
        assert gen._build("x", record, []).ai_generated is True


def test_provenance_always_names_the_source_question_first():
    """A rater checks two things: which paper it was modelled on, and which
    lesson grounds it. Both have to be in `derived_from`, in that order."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    q = gen._build("x", _source(question_id="Q7"), ["block-v1:a", "block-v1:b"])
    assert q.derived_from == ["Q7", "block-v1:a", "block-v1:b"]


# --- malformed output: one retry, then abstain -----------------------------


@pytest.mark.asyncio
async def test_malformed_output_gets_exactly_one_retry(monkeypatch):
    """First attempt unparseable, second valid — the turn succeeds."""
    router = _Router("not json at all", OK)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)
    assert router.calls == 2
    assert frames[-1].type == FrameType.DONE


@pytest.mark.asyncio
async def test_two_malformed_outputs_abstain(monkeypatch):
    """Never a third attempt, and never a question the contract rejected."""
    router = _Router("not json", '{"wrong_key": "x"}')
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)
    assert router.calls == 2
    assert frames[-1].error_code == ErrorCode.ABSTAINED
    assert not [f for f in frames if f.type == FrameType.TOKEN]


@pytest.mark.parametrize("raw", [
    None, "", "not json", "[1,2]", "null", '{"question": ""}',
    '{"question": "   "}', '{"question": 42}', '{"no_question": "x"}',
])
def test_unusable_model_output_is_rejected(raw):
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    assert gen._parse(raw) is None


@pytest.mark.parametrize("raw", [
    '{"question": "Q?"}',
    '```json\n{"question": "Q?"}\n```',
    '```\n{"question": "Q?"}\n```',
])
def test_a_fenced_code_block_is_still_usable(raw):
    """Models wrap JSON in fences often enough that refusing would spend the one
    retry on formatting instead of on content."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    assert gen._parse(raw) == "Q?"


@pytest.mark.asyncio
async def test_nothing_is_emitted_before_the_output_is_valid(monkeypatch):
    """The reason this pipeline does not stream tokens live: a student must never
    read a question we then discover is invalid, because it cannot be unsaid."""
    router = _Router("not json", "still not json")
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=router)

    frames = await _run(gen)
    assert [f.type for f in frames] == [FrameType.ERROR]


# --- marks and difficulty are validated ------------------------------------


def test_out_of_range_difficulty_is_refused_by_the_contract():
    """`difficulty` is 0..1 on both `QuestionRecord` and `PracticeQuestion`. A
    source record cannot carry 1.5, so the rubric's band check cannot be fed a
    value off its own scale."""
    with pytest.raises(Exception):
        _source(difficulty=1.5)
    with pytest.raises(Exception):
        PracticeQuestion(text="x", difficulty=1.5)
    with pytest.raises(Exception):
        PracticeQuestion(text="x", marks=-1)


def test_absent_marks_and_difficulty_survive_as_none(monkeypatch):
    """A freshly extracted pack often has neither. They must arrive as None, not
    as zero — the rubric reports "not run" on None and would score a fabricated
    0 as a real measurement."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    q = gen._build("x", _source(marks=None, difficulty=None), [])
    assert q.marks is None and q.difficulty is None
    assert band_of(q.difficulty) is None


# --- difficulty bands -------------------------------------------------------


@pytest.mark.parametrize("difficulty,expected", [
    (0.0, "easy"), (0.33, "easy"), (0.34, "medium"), (0.5, "medium"),
    (0.66, "medium"), (0.67, "hard"), (1.0, "hard"), (None, None),
])
def test_banding_is_one_shared_definition(difficulty, expected):
    """Lives in contracts because mastery will key on the same bands next phase,
    on the platform side, which cannot import the service."""
    assert band_of(difficulty) == expected


# --- §7.6 difficulty derivation --------------------------------------------
#
# Two signals, because each is wrong alone: marks alone rate a 15-mark
# "describe" as hard as a 15-mark "critically evaluate"; a verb alone rates a
# 2-mark "explain" the same as a 12-mark one.


@pytest.mark.parametrize("text,marks,expected_band", [
    ("State what the Open edX community is", 2, "easy"),
    ("Name two major members of the community", 3, "easy"),
    ("List the release stages", 1, "easy"),
    ("Describe one risk an operator accepts", 5, "medium"),
    ("Explain how the named release process helps", 10, "medium"),
    ("Critically evaluate the claim that governance is open", 15, "hard"),
    ("Evaluate the trade-offs of skipping a release", 12, "hard"),
    ("Justify your choice of deployment topology", 20, "hard"),
])
def test_difficulty_is_derived_from_marks_and_command_verb(text, marks, expected_band):
    assert band_of(derive_difficulty(text, marks)) == expected_band


def test_the_command_verb_separates_equally_weighted_questions():
    """The whole reason marks alone are not enough."""
    recall = derive_difficulty("Describe the release process", 15)
    judgement = derive_difficulty("Critically evaluate the release process", 15)
    assert judgement > recall


def test_the_marks_separate_questions_sharing_a_verb():
    """And the reason the verb alone is not enough either."""
    small = derive_difficulty("Explain one consequence", 2)
    large = derive_difficulty("Explain one consequence", 15)
    assert large > small


def test_an_unreadable_question_stays_unknown():
    """`None` in, `None` out — the same rule `band_of` and `CLOMastery.accuracy`
    follow. Defaulting would file every unparseable question into a band it was
    never assessed for."""
    assert derive_difficulty("", None) is None
    assert derive_difficulty("some fragment with no command verb", None) is None
    assert band_of(derive_difficulty("", None)) is None


def test_one_signal_is_enough():
    """A question missing marks or missing a recognised verb still gets what the
    surviving signal supports, rather than being discarded."""
    assert derive_difficulty("Explain the trade-off", None) is not None
    assert derive_difficulty("unreadable fragment", 12) is not None


def test_a_longer_command_phrase_wins_over_the_word_inside_it():
    """"Critically evaluate" must not be read as bare "evaluate"."""
    assert derive_difficulty("Critically evaluate X", 10) > derive_difficulty("Evaluate X", 10)


@pytest.mark.parametrize("marks", [1, 2, 5, 10, 15, 25, 100])
def test_a_derived_difficulty_always_fits_the_contract_range(marks):
    """`QuestionRecord.difficulty` is `ge=0.0, le=1.0`, so an overshoot would be
    refused at load time rather than caught here."""
    for verb in ("State", "Explain", "Critically evaluate", "zzz"):
        d = derive_difficulty(f"{verb} something", marks)
        assert d is None or 0.0 <= d <= 1.0


@pytest.mark.asyncio
async def test_a_requested_band_selects_a_matching_source(monkeypatch):
    from coursemate_service.ai import quiz_generator as qg

    easy = _source(question_id="EASY", difficulty=0.1)
    hard = _source(question_id="HARD", difficulty=0.9)
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))
    monkeypatch.setattr(qg.boundary, "search_past_questions", lambda *a, **k: [easy, hard])

    assert gen._find_source(_claims(), "CLO-1", "hard")[0].question_id == "HARD"
    assert gen._find_source(_claims(), "CLO-1", "easy")[0].question_id == "EASY"


@pytest.mark.asyncio
async def test_an_unmatched_band_falls_back_rather_than_abstaining(monkeypatch):
    """`difficulty` is derived and often absent, so a strict band filter would
    refuse to generate anything from a freshly extracted pack. The caller can see
    what band it actually got via `band_of`."""
    from coursemate_service.ai import quiz_generator as qg

    untagged = _source(question_id="UNTAGGED", difficulty=None)
    gen = _make(monkeypatch, source=untagged, context=_grounded(), router=_Router(OK))
    monkeypatch.setattr(qg.boundary, "search_past_questions", lambda *a, **k: [untagged])

    assert gen._find_source(_claims(), "CLO-1", "hard")[0].question_id == "UNTAGGED"


# --- the agent is not involved ---------------------------------------------


def test_the_generator_does_not_import_the_agent_package():
    """`agent_enabled=False` must still mean no agent code is imported. The
    generator is a pipeline; wiring its prompt into `agents/` would have quietly
    ended that property."""
    import sys

    for name in list(sys.modules):
        if name.startswith("coursemate_service.agents"):
            del sys.modules[name]

    import importlib

    importlib.reload(importlib.import_module("coursemate_service.ai.quiz_generator"))
    assert not [m for m in sys.modules if m.startswith("coursemate_service.agents")]


# --- Phase 1B decision 1: generation never degrades to `cheap` --------------


def test_generation_fallback_excludes_cheap():
    """`cheap` shares the primary's vendor, so it does not survive the outage a
    fallback exists for — and a generated question reaches a student ungated
    because the STRONG model's output was measured. Serving a weaker model
    quietly ships unmeasured output under someone else's measurement."""
    from coursemate_service.ai.client import (
        build_fallback_chain,
        build_generation_fallback_chain,
    )

    models = [{"model_name": n, "litellm_params": {}} for n in ("strong", "cheap", "fallback")]
    assert build_fallback_chain(models) == [{"strong": ["fallback", "cheap"]}]
    assert build_generation_fallback_chain(models) == [{"strong": ["fallback"]}]


def test_generation_has_no_fallback_when_no_second_vendor_is_configured():
    """Empty chain, so a `strong` outage becomes UNAVAILABLE rather than a silent
    downgrade. That is the honest outcome."""
    from coursemate_service.ai.client import build_generation_fallback_chain

    only = [{"model_name": n, "litellm_params": {}} for n in ("strong", "cheap")]
    assert build_generation_fallback_chain(only) == []


@pytest.mark.asyncio
async def test_the_generation_chain_is_what_reaches_the_router(monkeypatch):
    """Not just that the helper is right — that the pipeline passes it."""
    seen = {}

    class _Spy(_Router):
        async def acompletion(self, **kw):
            seen.update(kw)
            return await super().acompletion(**kw)

    from coursemate_service.ai import quiz_generator as qg

    monkeypatch.setattr(qg, "build_model_list", lambda: [
        {"model_name": n, "litellm_params": {}} for n in ("strong", "cheap", "fallback")
    ])
    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Spy(OK))
    await _run(gen)

    assert seen["fallbacks"] == [{"strong": ["fallback"]}]
    assert "cheap" not in str(seen["fallbacks"])


# --- Phase 1B decision 3: near-duplicate is caught at serve time ------------


@pytest.mark.asyncio
async def test_a_reprinted_source_question_is_rejected_then_retried(monkeypatch):
    """Handing a student a real exam question labelled AI-generated is a false
    claim, so it spends the retry exactly as malformed output does."""
    src = _source()
    reprint = '{"question": "%s"}' % src.text
    router = _Router(reprint, OK)
    gen = _make(monkeypatch, source=src, context=_grounded(), router=router)

    frames = await _run(gen)
    assert router.calls == 2
    assert frames[-1].type == FrameType.DONE
    assert src.text not in _text(frames)


@pytest.mark.asyncio
async def test_two_reprints_abstain_rather_than_serving_one(monkeypatch):
    src = _source()
    reprint = '{"question": "%s"}' % src.text
    gen = _make(monkeypatch, source=src, context=_grounded(), router=_Router(reprint, reprint))

    frames = await _run(gen)
    assert frames[-1].error_code == ErrorCode.ABSTAINED
    assert not [f for f in frames if f.type == FrameType.TOKEN]


def test_reprint_detection_catches_the_source_but_not_a_new_question():
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    src = _source()
    assert gen._is_reprint(src.text, [src]) == "Q1"
    assert gen._is_reprint(
        "Describe how a time quantum affects average turnaround under round robin.", [src]
    ) is None


def test_reprint_detection_has_the_documented_blind_spot():
    """Token overlap catches reprinting, not rewording — the same floor the
    rubric and verify.py use. Asserted so the limitation stays true if the
    threshold moves, rather than quietly becoming a false claim."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    reworded = "Between a pair of tasks, describe the circumstances in which "                "circular waiting emerges and cannot resolve."
    assert gen._is_reprint(reworded, [_source()]) is None


# --- Phase 1B quality fix: the prompt must state the target ----------------
#
# `clo_alignment` measured 0.611 on the 20-question eval, with misses landing on
# arbitrary outcomes. Cause: the prompt named a source question and up to five
# lesson chunks but never said which learning outcome the new question had to
# assess, so the model anchored on whatever the retrieved context emphasised.


def _prompt(gen, **kw) -> str:
    msgs = gen._messages(_source(), _grounded(), **kw)
    return "\n".join(m["content"] for m in msgs)


def test_the_prompt_names_the_target_outcome():
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    text = _prompt(gen, outcome_id="CLO-1", outcome_text="Deadlock and concurrency")

    assert "TARGET LEARNING OUTCOME: CLO-1 — Deadlock and concurrency" in text
    assert "must assess THIS outcome" in text


def test_the_prompt_names_the_target_band():
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    assert "Target difficulty: hard" in _prompt(gen, band="hard")


def test_the_prompt_tells_the_model_to_ignore_off_topic_context():
    """The drift mechanism: five chunks of lesson material can cover topics the
    requested outcome does not. Naming the outcome is only half the fix — the
    other half is saying what to do with the rest."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    assert "ignore them" in _prompt(gen, outcome_id="CLO-1")


def test_the_prompt_does_not_teach_the_rubric_its_answer():
    """Stating the requirement is a fix; listing the command verbs the rubric
    scores would be writing to the metric. The band is named, the vocabulary is
    not."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    text = _prompt(gen, outcome_id="CLO-1", band="hard").lower()

    for verb in ("evaluate", "analyse", "critically", "justify", "propose"):
        assert verb not in text, f"prompt hands the model the rubric's verb {verb!r}"


def test_a_missing_outcome_description_degrades_rather_than_fails():
    """`get_clos` can return nothing for an unloaded pack. The id alone still
    anchors the model; the generation must not fail for want of prose."""
    gen = QuizGenerator(context_provider=_Ctx(_grounded()))
    text = _prompt(gen, outcome_id="CLO-1", outcome_text=None)

    assert "TARGET LEARNING OUTCOME: CLO-1" in text
    assert "—" not in text.split("TARGET LEARNING OUTCOME: CLO-1")[1].split("\n")[0]


@pytest.mark.asyncio
async def test_a_denied_clo_lookup_does_not_fail_the_generation(monkeypatch):
    """The extra boundary read is a prompt improvement, not a dependency. If it
    is denied, the turn still generates — just with a weaker prompt."""
    from coursemate_service.ai import quiz_generator as qg
    from coursemate_service.boundary.impl import AuthorizationError

    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))

    def denied(*a, **k):
        raise AuthorizationError("nope")

    monkeypatch.setattr(qg.boundary, "get_clos", denied)
    assert (await _run(gen))[-1].type == FrameType.DONE


@pytest.mark.asyncio
async def test_the_requested_band_reaches_the_prompt_not_just_the_filter(monkeypatch):
    """The band was already used to PICK a source; the bug was that it never
    reached the model. This pins the whole path."""
    seen = {}

    class _Spy(_Router):
        async def acompletion(self, **kw):
            seen["messages"] = kw["messages"]
            return await super().acompletion(**kw)

    from coursemate_service.ai import quiz_generator as qg

    gen = _make(monkeypatch, source=_source(), context=_grounded(), router=_Spy(OK))
    monkeypatch.setattr(qg.boundary, "get_clos", lambda *a, **k: [])
    await _run(gen, difficulty_band="hard")

    joined = "\n".join(m["content"] for m in seen["messages"])
    assert "Target difficulty: hard" in joined
    assert "TARGET LEARNING OUTCOME: CLO-1" in joined


# --- closing the mastery loop (D2) ------------------------------------------
#
# `record_attempt` requires a `question_id`, and until 2026-08-12 the practice
# stream sent only the question TEXT and the source paper's name. Nothing could
# call the mastery write, so the counters that rank a study plan were read by
# four components and written by none. These frames are what make the loop
# closable at all.


@pytest.mark.asyncio
async def test_done_carries_the_source_question_id(monkeypatch):
    gen = _make(monkeypatch, source=_source(question_id="Q-42"), context=_grounded(),
                router=_Router(OK))
    done = (await _run(gen))[-1]

    assert done.type == FrameType.DONE
    assert done.question_id == "Q-42"


@pytest.mark.asyncio
async def test_done_carries_the_band_actually_used_not_the_one_requested(monkeypatch):
    """`_pick` falls back to the closest available question when a course has
    nothing in the asked-for band. Recording the REQUEST would bucket a
    student's counters under a difficulty they never practised."""
    gen = _make(monkeypatch, source=_source(difficulty=0.9), context=_grounded(),
                router=_Router(OK))

    done = (await _run(gen, difficulty_band="easy"))[-1]

    assert done.difficulty_band == band_of(0.9)
    assert done.difficulty_band != "easy"


@pytest.mark.asyncio
async def test_an_unscored_question_reports_no_band_rather_than_easy(monkeypatch):
    """`band_of` is None-in/None-out: an unknown difficulty is unknown, not
    easy. `record_attempt` accepts "" for exactly this case."""
    gen = _make(monkeypatch, source=_source(difficulty=None), context=_grounded(),
                router=_Router(OK))
    done = (await _run(gen))[-1]

    assert done.difficulty_band is None


@pytest.mark.asyncio
async def test_an_abstention_carries_no_question_id(monkeypatch):
    """No question was shown, so there is nothing to record an attempt against.
    A question_id here would let the browser offer a self-assessment for a
    question that does not exist."""
    weak = ContextResult(chunks=[], top_score=0.0)
    gen = _make(monkeypatch, source=_source(), context=weak, router=_Router(OK))

    frames = await _run(gen)

    assert frames[-1].type == FrameType.ERROR
    assert all(getattr(f, "question_id", None) is None for f in frames)


def test_chat_never_sets_the_practice_fields():
    """The two fields are scoped to one frame of one path, like `citation` and
    `truncated`. A chat DONE frame setting them would mean the chat UI could
    offer a mastery write against a question nobody generated."""
    import inspect

    from coursemate_service.ai import pipeline

    source = inspect.getsource(pipeline)
    assert "question_id=" not in source
    assert "difficulty_band=" not in source


# --- provenance: "Derived from" must mean "this contributed" ----------------
#
# `quiz_generator` emitted a CITATION for every retrieved chunk and carried all
# of them in `derived_from`, so the card's "Derived from" line meant *we
# searched this* rather than *this contributed*. `pipeline.py:288` had already
# fixed the same thing for chat answers, with the same reasoning.
#
# **Measured before adopting the rule** (eval/measure_question_grounding.py, real
# generations against OEX101). Questions do share fewer content words with their
# chunks than prose does — median 6 against 20 — but the floor was 3, well clear
# of `supporting_chunks`' threshold of 1, so nothing loses a citation. On that
# data the narrowing selects nothing out; it is a guard for when retrieval
# returns more than the question uses, which `rerank_top_k=3` currently prevents.
# These tests build that case explicitly rather than waiting for it.


def _multi_chunk(*specs: tuple[str, str]) -> ContextResult:
    """A context of several chunks: (usage_key, text) each."""
    return ContextResult(
        chunks=[
            ContextChunk(text=text,
                         citation=Citation(usage_key=key, display_name=key),
                         score=0.9)
            for key, text in specs
        ],
        top_score=0.9,
    )


async def test_a_retrieved_chunk_the_question_never_used_is_not_cited(monkeypatch):
    """The defect, stated directly. An unrelated chunk shares no content word
    with the question, so it must not appear under "Derived from"."""
    context = _multi_chunk(
        ("block-v1:deadlock", "A deadlock arises when processes wait in a circular chain."),
        ("block-v1:unrelated", "Photosynthesis converts light into chemical energy."),
    )
    gen = _make(monkeypatch, source=_source(), context=context, router=_Router(OK))

    frames = await _run(gen)
    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]

    assert "block-v1:unrelated" not in cited, (
        "a chunk sharing nothing with the question is shown as a source"
    )
    assert "block-v1:deadlock" in cited, "the chunk it did draw on was dropped"


def test_supporting_selects_only_the_chunks_the_question_used():
    """`_supporting` in isolation — the selection `derived_from` is built from.

    Tested directly rather than by spying on `_build`. The spy version passed
    alone and failed in the suite: patching a method on the class is sensitive
    to what every other test in the file has already done to it, and a test that
    depends on its neighbours is worse than no test.
    """
    from coursemate_service.ai.quiz_generator import QuizGenerator

    context = _multi_chunk(
        ("block-v1:deadlock", "A deadlock arises when processes wait in a circular chain."),
        ("block-v1:unrelated", "Photosynthesis converts light into chemical energy."),
    )
    keep = QuizGenerator._supporting(
        "Explain how a deadlock can arise between two processes.", context
    )
    keys = [c.citation.usage_key for c in keep]

    assert keys == ["block-v1:deadlock"]


def test_derived_from_is_built_from_the_same_selection_that_is_cited():
    """The stored record and the displayed line cannot disagree about
    provenance — a rater checking `derived_from` must see what the student saw.

    A source scan, because both halves are one expression in the code: the list
    passed to `_build` is derived from `supporting`, and `supporting` is what the
    CITATION loop iterates. Asserting that once is stronger than asserting two
    runtime values happen to match."""
    import inspect

    from coursemate_service.ai import quiz_generator as qg

    src = inspect.getsource(qg)
    assert "supporting = self._supporting(text, context)" in src
    assert "[c.citation.usage_key for c in supporting]" in src, (
        "derived_from no longer uses the supporting selection"
    )
    assert "for chunk in supporting:" in src, (
        "the citation loop no longer iterates the supporting selection"
    )


async def test_every_supporting_chunk_is_still_cited(monkeypatch):
    """Narrowing must not become dropping. Two chunks that both contribute are
    both shown."""
    context = _multi_chunk(
        ("block-v1:a", "A deadlock arises when processes wait in a circular chain."),
        ("block-v1:b", "Circular wait is one of the four deadlock conditions."),
    )
    gen = _make(monkeypatch, source=_source(), context=context, router=_Router(OK))

    frames = await _run(gen)
    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]

    assert "block-v1:a" in cited
    assert "block-v1:b" in cited


async def test_mandatory_citation_survives_when_nothing_overlaps(monkeypatch):
    """§8.5's promise, and the case most likely to be broken by narrowing.

    When the question shares no content word with ANY chunk, `supporting_chunks`
    returns every index rather than none — because a question that can cite
    nothing was supposed to abstain upstream, and showing zero sources would
    hide provenance exactly when a student most needs it."""
    context = _multi_chunk(
        ("block-v1:x", "Photosynthesis converts light into chemical energy."),
        ("block-v1:y", "Mitochondria produce ATP for the cell."),
    )
    gen = _make(monkeypatch, source=_source(), context=context, router=_Router(OK))

    frames = await _run(gen)
    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]

    assert "block-v1:x" in cited and "block-v1:y" in cited, (
        "narrowing dropped to zero lesson citations instead of falling back to all"
    )


async def test_the_source_paper_is_always_cited(monkeypatch):
    """It is not a retrieved chunk and is never subject to the overlap rule: the
    question was modelled on that paper whatever words it ended up using."""
    context = _multi_chunk(
        ("block-v1:unrelated", "Photosynthesis converts light into chemical energy."),
    )
    gen = _make(monkeypatch, source=_source(source_doc_id="paper-2024.pdf"),
                context=context, router=_Router(OK))

    frames = await _run(gen)
    cited = [f.citation.usage_key for f in frames if f.type == FrameType.CITATION]

    # The paper is identified by `source_doc_id`, not `question_id` — the id the
    # citation carries is the DOCUMENT, which is what a student can go and read.
    assert cited[0] == "paper-2024.pdf", "the source paper is no longer cited first"


def test_the_generator_reuses_the_chat_grounding_rule():
    """One implementation, not two that agree today. A second copy would drift,
    and this repo has already paid for that once with the confidence gate."""
    import inspect

    from coursemate_service.ai import quiz_generator as qg

    source = inspect.getsource(qg)
    assert "supporting_chunks" in source, "the generator no longer uses the shared rule"
    assert "for chunk in context.chunks:\n            yield StreamFrame" not in source, (
        "the generator emits every retrieved chunk again"
    )


# --- citation PRECISION: rank, then keep the top band -----------------------
#
# `supporting_chunks` asks "does this share ANY content word", which in a
# single-domain corpus is nearly always yes — `community`, `edx`, `open` recur
# throughout an Open edX course. Measured over 20 real generations, 60 citation
# pairs hand-labelled against the chunk text: 21 genuinely supporting, 36
# irrelevant, 3 unclear. A 65% false-citation rate under a line reading
# "Derived from", on the one feature §9.0 lets reach a student ungated BECAUSE
# it is cited.
#
# Ranking on the same overlap and keeping chunks within `_TOP_SHARE` of the best
# scored 21 citations for the 21 genuine chunks: 0% false, 100% recall.
#
# The scores below are asserted exactly rather than approximately, because the
# whole rule is a ratio and a fixture that drifts by one shared word changes
# which side of the band it lands on.


def _scored_chunks(question: str, *texts: str):
    """A context plus the raw overlap score of each chunk against `question`."""
    from coursemate_service.ai.verify import content_terms

    ctx = _multi_chunk(*[(f"block-v1:c{i}", t) for i, t in enumerate(texts)])
    qt = content_terms(question)
    return ctx, [len(qt & content_terms(t)) for t in texts]


def test_1_an_irrelevant_lower_scoring_chunk_is_excluded():
    """The defect, at the unit that decides it. A chunk sharing only incidental
    vocabulary scores below the band and is dropped."""
    from coursemate_service.ai.quiz_generator import QuizGenerator

    q = "Explain why two processes each holding one resource cannot proceed."
    ctx, scores = _scored_chunks(
        q,
        "Two processes each holding one resource cannot proceed; neither can "
        "acquire the resource the other holds.",
        "The community publishes one blog post about processes each release.",
    )
    assert scores[0] > scores[1], f"fixture no longer discriminates: {scores}"

    keep = QuizGenerator._supporting(q, ctx)
    keys = [c.citation.usage_key for c in keep]
    assert keys == ["block-v1:c0"], f"scores={scores} kept={keys}"


def test_2_a_second_genuine_chunk_at_exactly_the_band_is_retained():
    """The one case in twenty that best-only would lose: a real second source.

    Constructed so the second chunk scores EXACTLY `_TOP_SHARE` of the best —
    9 of 10 shared terms at 0.90 — because that is the boundary the rule turns
    on, and an inequality is only pinned by testing its edge."""
    from coursemate_service.ai.quiz_generator import _TOP_SHARE, QuizGenerator

    q = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    ctx, scores = _scored_chunks(
        q,
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet",
        "alpha bravo charlie delta echo foxtrot golf hotel india",
    )
    assert scores == [10, 9], f"fixture drifted: {scores}"
    assert scores[1] / scores[0] == _TOP_SHARE, "fixture is not exactly at the band"

    keys = [c.citation.usage_key for c in QuizGenerator._supporting(q, ctx)]
    assert keys == ["block-v1:c0", "block-v1:c1"], (
        f"a genuine second source exactly at the band was dropped: {keys}"
    )


def test_3_a_chunk_below_the_band_is_excluded():
    """One shared word lower — 8 of 10, i.e. 80% — and it falls outside.

    Pairs with the test above: together they pin the boundary from both sides,
    which a single test on one side of it cannot do."""
    from coursemate_service.ai.quiz_generator import QuizGenerator

    q = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    ctx, scores = _scored_chunks(
        q,
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet",
        "alpha bravo charlie delta echo foxtrot golf hotel",
    )
    assert scores == [10, 8], f"fixture drifted: {scores}"

    keys = [c.citation.usage_key for c in QuizGenerator._supporting(q, ctx)]
    assert keys == ["block-v1:c0"], f"a chunk at 80% of best was cited: {keys}"


async def test_4_a_single_source_question_produces_one_citation(monkeypatch):
    """19 of the 20 measured questions were single-source. End to end, that must
    now yield ONE lesson citation rather than the retrieved three."""
    context = _multi_chunk(
        ("block-v1:on-topic",
         "Given two processes holding one resource each, neither can proceed "
         "because each waits on the resource the other holds."),
        ("block-v1:off-1", "The community blog is published on the site."),
        ("block-v1:off-2", "Working groups meet on a monthly calendar."),
    )
    gen = _make(monkeypatch, source=_source(), context=context, router=_Router(OK))

    frames = await _run(gen)
    lessons = [f.citation.usage_key for f in frames
               if f.type == FrameType.CITATION
               and f.citation.usage_key.startswith("block-v1:")]

    assert lessons == ["block-v1:on-topic"], f"expected one lesson citation, got {lessons}"


async def test_5_zero_overlap_still_cites_everything(monkeypatch):
    """§8.5 unchanged. With no shared word anywhere, `supporting_chunks` falls
    back to every chunk and the ranking must not narrow that to nothing.

    This pins the BEHAVIOUR, not the branch that implements it. Deleting the
    explicit `best == 0` guard leaves this passing, because the band multiplies
    rather than divides and `0 >= 0.9 * 0` keeps everything anyway — confirmed
    by mutation. The behaviour is what §8.5 requires; the guard exists so that
    rewriting the band as a division stays safe."""
    context = _multi_chunk(
        ("block-v1:x", "Photosynthesis converts light into chemical energy."),
        ("block-v1:y", "Mitochondria produce ATP for the cell."),
    )
    gen = _make(monkeypatch, source=_source(), context=context, router=_Router(OK))

    frames = await _run(gen)
    lessons = [f.citation.usage_key for f in frames
               if f.type == FrameType.CITATION
               and f.citation.usage_key.startswith("block-v1:")]

    assert set(lessons) == {"block-v1:x", "block-v1:y"}, (
        f"the mandatory-citation fallback was narrowed: {lessons}"
    )


def test_6_the_band_is_local_and_chat_grounding_is_untouched():
    """The rule lives in the generator. `verify.py` is shared with chat, whose
    citations were never measured here, so widening the change to it would be
    claiming evidence that does not exist."""
    import inspect

    from coursemate_service.ai import quiz_generator as qg
    from coursemate_service.ai import verify

    assert "_TOP_SHARE" in inspect.getsource(qg)
    assert "_TOP_SHARE" not in inspect.getsource(verify), (
        "the band leaked into shared chat grounding"
    )
    # supporting_chunks is still the gate that runs first.
    assert "supporting_chunks(question_text" in inspect.getsource(qg)


def test_the_band_is_not_idf_weighted():
    """Measured and rejected, so it is pinned rather than left to be rediscovered.

    On the same 60 pairs idf weighting could NOT separate the cases: an
    irrelevant chunk reached 91% of best while a genuine one sat at 81%. Raw
    counting separated cleanly at (80%, 90%]. Someone reasoning from first
    principles would reach for idf, so the negative result earns a test.

    Comments and the docstring are stripped first. The method EXPLAINS that idf
    was rejected, so scanning raw text matched the explanation rather than any
    code — the same trap `test_studio_settings.py` strips a docstring to avoid,
    and the third time it has been walked into in this file's history."""
    import inspect
    import re

    from coursemate_service.ai.quiz_generator import QuizGenerator

    body = inspect.getsource(QuizGenerator._supporting)
    code = re.sub(r'"""[\s\S]*?"""', "", body, count=1)   # docstring
    code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)  # comments

    assert "idf" not in code.lower(), "idf weighting crept in"
    assert "len(content_terms(" in code, "the score is no longer a raw term count"


# --- F1: the reference answer is fenced off from generation ------------------
#
# The field exists on the SOURCE question. Three things must never see it: the
# prompt, the retrieval query, and the overlap scorer that picks citations. Each
# is proven separately rather than by inspection, because the isolation is
# structural (field-by-field access) and a future refactor to `model_dump()`
# would break all three at once and silently.

REF = "MODEL ANSWER: circular wait plus mutual exclusion plus hold-and-wait."


def _source_with_answer(**kw) -> QuestionRecord:
    return _source(
        reference_answer=REF,
        reference_answer_source_doc_id="final-2024-marking-scheme.pdf",
        reference_answer_page=11,
        **kw,
    )


def test_the_reference_answer_never_enters_the_prompt(monkeypatch):
    gen = _make(monkeypatch, source=_source_with_answer(), context=_grounded(),
                router=_Router(OK))
    messages = gen._messages(_source_with_answer(), _grounded(),
                             outcome_id="CLO-1", outcome_text="Deadlock", band="medium")

    blob = " ".join(m["content"] for m in messages)
    assert REF not in blob, "the model was shown the examiner's answer"
    assert "circular wait plus mutual exclusion" not in blob
    assert "marking-scheme" not in blob
    # ...and the things that SHOULD be there still are.
    assert _source().text in blob


@pytest.mark.asyncio
async def test_the_reference_answer_reaches_no_model_call(monkeypatch):
    """End to end: whatever the router is handed, across every attempt."""
    seen: list[str] = []

    class _Spy(_Router):
        async def acompletion(self, **kw):
            seen.append(str(kw.get("messages", "")))
            return await super().acompletion(**kw)

    gen = _make(monkeypatch, source=_source_with_answer(), context=_grounded(),
                router=_Spy(OK))
    await _run(gen)

    assert seen, "no model call was made; the test proves nothing"
    assert not any(REF in s for s in seen)


def test_the_reference_answer_never_reaches_the_overlap_scorer(monkeypatch):
    """`_supporting` scores the GENERATED question against lesson chunks. Feeding
    it examiner prose would corrupt the measured 0.90 rule with text that is not
    lesson material at all."""
    gen = _make(monkeypatch, source=_source_with_answer(), context=_grounded(),
                router=_Router(OK))
    seen: list[str] = []

    original = gen._supporting

    def spy(question_text, context):
        seen.append(question_text)
        seen.extend(c.text for c in context.chunks)
        return original(question_text, context)

    monkeypatch.setattr(gen, "_supporting", spy)
    gen._supporting("A brand new question about deadlock.", _grounded())

    assert seen, "the spy never ran"
    assert not any(REF in s for s in seen)


@pytest.mark.asyncio
async def test_the_citations_are_identical_with_and_without_an_answer(monkeypatch):
    """The strongest form of the claim: the cited set is byte-identical whether
    or not the source carries a reference answer."""
    plain = _make(monkeypatch, source=_source(), context=_grounded(), router=_Router(OK))
    without = [f.citation.usage_key for f in await _run(plain)
               if f.type == FrameType.CITATION]

    withans = _make(monkeypatch, source=_source_with_answer(), context=_grounded(),
                    router=_Router(OK))
    with_ = [f.citation.usage_key for f in await _run(withans)
             if f.type == FrameType.CITATION]

    assert without == with_ == ["final-2024.pdf", "block-v1:lesson"]


@pytest.mark.asyncio
async def test_the_generated_question_carries_no_reference_answer(monkeypatch):
    """A generated question is new. Nothing has published a model answer for it,
    so it must not inherit the source question's."""
    gen = _make(monkeypatch, source=_source_with_answer(), context=_grounded(),
                router=_Router(OK))
    q = gen._build("A new question.", _source_with_answer(), ["block-v1:lesson"])

    assert getattr(q, "reference_answer", None) in (None, "")


def test_has_reference_answer_treats_blank_as_absent():
    assert _source().has_reference_answer is False
    assert _source(reference_answer="   ").has_reference_answer is False
    assert _source_with_answer().has_reference_answer is True
