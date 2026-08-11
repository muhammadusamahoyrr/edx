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
from coursemate_contracts.examprep import ExamType, PracticeQuestion, QuestionRecord, band_of
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

    def fetch_sync(self, question, claims, limit=None):  # noqa: ARG002
        return self.result


class _Router:
    """Returns a scripted sequence of raw model outputs."""

    def __init__(self, *payloads: str | None):
        self.payloads = list(payloads)
        self.calls = 0

    async def acompletion(self, **kw):  # noqa: ARG002
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
        build_fallback_chain, build_generation_fallback_chain,
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
