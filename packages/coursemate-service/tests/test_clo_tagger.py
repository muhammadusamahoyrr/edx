"""Phase 2B — offline CLO tagging.

§7.5 makes a tag a *proposal*: AI-suggested, correctable by an instructor or the
student. That is only safe while a bad proposal is never dressed up as a good
one, so almost every test here is about a refusal. A wrongly tagged question
sends a student to revise the wrong topic; an untagged one is still practisable
and still searchable by every other filter.
"""

from __future__ import annotations

import json

import pytest
from coursemate_contracts.examprep import CLO, ExamPrepPack, QuestionRecord
from coursemate_service.ai.clo_tagger import (
    LOW_CONFIDENCE,
    MIN_CONFIDENCE,
    ProviderFailure,
    TaggingReport,
    _decide,
    tag_pack,
)

OFFERING = "course-v1:OpenedX+OEX101+2023"
TENANT = "default"
ALLOWED = {"CLO-1", "CLO-2"}


def _q(qid: str, **kw) -> QuestionRecord:
    base = {
        "question_id": qid, "tenant": TENANT, "offering_id": OFFERING,
        "source_doc_id": "final-2024.pdf", "text": f"Explain topic {qid} in detail.",
        "marks": 10, "confidence": 1.0,
    }
    return QuestionRecord(**{**base, **kw})


def _pack(*questions, clos=None) -> ExamPrepPack:
    return ExamPrepPack(
        offering_id=OFFERING, tenant=TENANT,
        clos=clos if clos is not None else [
            CLO(clo_id="CLO-1", text="Community", confirmed_by="dr-lee"),
            CLO(clo_id="CLO-2", text="Releases", confirmed_by="dr-lee"),
        ],
        questions=list(questions),
    )


class _Router:
    """Scripted replies. An Exception in the script raises from the call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def acompletion(self, **kw):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else None
        if isinstance(r, Exception):
            raise r
        from types import SimpleNamespace

        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])


def _with(monkeypatch, router):
    from coursemate_service.ai import clo_tagger as t

    monkeypatch.setattr(t, "get_router", lambda: router)


def ok(clo="CLO-1", conf=0.9) -> str:
    return json.dumps({"clo_id": clo, "confidence": conf})


# --- the happy path --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_tag_is_applied_with_its_confidence(monkeypatch):
    _with(monkeypatch, _Router(ok("CLO-2", 0.92)))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    q = tagged.questions[0]
    assert q.clo_id == "CLO-2"
    assert q.clo_confidence == 0.92
    assert q.low_confidence_flag is False
    assert report.as_dict() == {
        "total": 1, "tagged": 1, "low_confidence": 0, "untagged": 0,
        "failed": 0, "already_tagged": 0,
    }


@pytest.mark.asyncio
async def test_the_extractor_confidence_is_not_overwritten(monkeypatch):
    """Two different questions — "did we read it right?" and "did we file it
    right?" — that one number cannot answer. Overwriting `confidence` would
    destroy the signal Phase 2A added."""
    _with(monkeypatch, _Router(ok()))
    tagged, _ = await tag_pack(_pack(_q("Q1", confidence=0.6)))

    assert tagged.questions[0].confidence == 0.6      # extraction, untouched
    assert tagged.questions[0].clo_confidence == 0.9  # tagging, separate


# --- refusals: every uncertainty leaves the question untagged ---------------


@pytest.mark.asyncio
async def test_an_outcome_from_another_offering_is_refused(monkeypatch):
    """Scope is enforced against the pack's own CLO list. An id we did not offer
    may belong to another course, and accepting it would file a question under an
    outcome this offering does not have."""
    _with(monkeypatch, _Router(ok("CLO-99", 0.99)))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert report.untagged == 1 and report.tagged == 0
    assert "not in this offering" in report.outcomes[0].reason


@pytest.mark.asyncio
async def test_an_explicit_null_is_an_answer_not_a_malfunction(monkeypatch):
    """The prompt asks for null when nothing fits. Treating that as a parse
    failure would retry a correct refusal and spend money reaching it again."""
    router = _Router(json.dumps({"clo_id": None, "confidence": 0.1}))
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert report.untagged == 1 and report.failed == 0
    assert router.calls == 1, "a refusal must not be retried"


@pytest.mark.asyncio
@pytest.mark.parametrize("stringly_null", ["null", "NULL", "none", "N/A", "", "  "])
async def test_a_stringly_typed_null_is_read_as_a_refusal(monkeypatch, stringly_null):
    """`"null"` as a STRING is how a small model says null — observed from
    qwen2.5:7b on the first real pack. The outcome was already safe, but it was
    reported as "outcome 'null' is not in this offering", which tells the operator
    the model hallucinated an id when it actually refused correctly."""
    _with(monkeypatch, _Router(json.dumps({"clo_id": stringly_null, "confidence": 0.3})))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert report.untagged == 1
    assert report.outcomes[0].reason == "model found no fitting outcome"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    "not json at all", "[1,2]", "null", '"a string"', "{}",
    '{"confidence": 0.9}',                     # no clo_id key
    '{"clo_id": 7, "confidence": 0.9}',        # wrong type
    '{"clo_id": "CLO-1"}',                     # no confidence
    '{"clo_id": "CLO-1", "confidence": 5}',    # out of range
    '{"clo_id": "CLO-1", "confidence": "high"}',
    None,
])
async def test_malformed_output_leaves_the_question_untagged(monkeypatch, reply):
    _with(monkeypatch, _Router(reply))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert tagged.questions[0].clo_confidence is None
    assert report.tagged == 0


@pytest.mark.asyncio
async def test_a_barely_guessing_tag_is_discarded_not_flagged(monkeypatch):
    """Below MIN_CONFIDENCE the model is not making a proposal."""
    _with(monkeypatch, _Router(ok("CLO-1", MIN_CONFIDENCE - 0.05)))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert report.untagged == 1


@pytest.mark.asyncio
async def test_a_pack_with_no_clos_tags_nothing(monkeypatch):
    """Without a confirmed vocabulary there is no proposal to make, only a
    guess. §7.3: assisted, never asserted."""
    router = _Router(ok())
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1"), clos=[]))

    assert tagged.questions[0].clo_id is None
    assert router.calls == 0, "the model must not be consulted with no vocabulary"
    assert report.untagged == 1


# --- low confidence: kept, but flagged -------------------------------------


@pytest.mark.asyncio
async def test_a_low_confidence_tag_is_kept_and_flagged(monkeypatch):
    """§7.5 makes it a proposal, so a weak one still beats none — provided the
    student is told it is weak."""
    _with(monkeypatch, _Router(ok("CLO-1", LOW_CONFIDENCE - 0.05)))
    tagged, report = await tag_pack(_pack(_q("Q1")))

    q = tagged.questions[0]
    assert q.clo_id == "CLO-1"
    assert q.low_confidence_flag is True
    assert report.tagged == 1 and report.low_confidence == 1


@pytest.mark.asyncio
async def test_a_confident_tag_does_not_clear_a_bad_extraction_flag(monkeypatch):
    """The flag answers "should I trust this item?". A shaky parse is as good a
    reason to doubt it as a shaky tag, so tagging must not clear it."""
    _with(monkeypatch, _Router(ok("CLO-1", 0.99)))
    tagged, _ = await tag_pack(_pack(_q("Q1", confidence=0.4, low_confidence_flag=True)))

    assert tagged.questions[0].clo_id == "CLO-1"
    assert tagged.questions[0].low_confidence_flag is True


# --- provider failure: bounded retry, then explicitly retryable -------------


@pytest.mark.asyncio
async def test_a_transient_provider_failure_is_retried(monkeypatch):
    router = _Router(RuntimeError("502"), ok("CLO-1", 0.9))
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert router.calls == 2
    assert tagged.questions[0].clo_id == "CLO-1"
    assert report.failed == 0


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_question_stays_untagged(monkeypatch):
    """"The provider is down" does not improve by being asked more often, and an
    unbounded retry over a whole pack is how an offline job runs all night."""
    router = _Router(*[RuntimeError("down")] * 10)
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1")), max_attempts=3)

    assert router.calls == 3
    assert tagged.questions[0].clo_id is None
    assert report.failed == 1 and report.tagged == 0
    assert report.outcomes[0].retryable is True


@pytest.mark.asyncio
async def test_a_refusal_is_not_marked_retryable(monkeypatch):
    """Re-running a refusal would spend money reaching the same answer."""
    _with(monkeypatch, _Router(ok("CLO-99", 0.99)))
    _, report = await tag_pack(_pack(_q("Q1")))

    assert report.outcomes[0].status == "untagged"
    assert report.outcomes[0].retryable is False


@pytest.mark.asyncio
async def test_one_dead_question_does_not_starve_the_rest(monkeypatch):
    """A failure goes to the BACK of the queue, so a whole-pack outage does not
    spin on the first question while the others wait."""
    router = _Router(RuntimeError("x"), ok("CLO-1"), ok("CLO-2"), ok("CLO-1"))
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1"), _q("Q2"), _q("Q3")))

    assert report.tagged == 3
    assert {q.clo_id for q in tagged.questions} <= ALLOWED


@pytest.mark.asyncio
async def test_no_provider_configured_fails_rather_than_guessing(monkeypatch):
    from coursemate_service.ai import clo_tagger as t

    def boom():
        from coursemate_service.ai.client import NoModelConfigured

        raise NoModelConfigured("none")

    monkeypatch.setattr(t, "get_router", boom)
    tagged, report = await tag_pack(_pack(_q("Q1")))

    assert tagged.questions[0].clo_id is None
    assert report.failed == 1 and report.tagged == 0


# --- idempotency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_already_tagged_questions_are_never_reconsidered(monkeypatch):
    router = _Router(ok("CLO-2", 0.99))
    _with(monkeypatch, router)
    tagged, report = await tag_pack(_pack(_q("Q1", clo_id="CLO-1", clo_confidence=0.8)))

    assert router.calls == 0
    assert tagged.questions[0].clo_id == "CLO-1"
    assert tagged.questions[0].clo_confidence == 0.8
    assert report.already_tagged == 1 and report.tagged == 0


@pytest.mark.asyncio
async def test_a_rerun_retries_only_the_failures(monkeypatch):
    """The recovery path: an outage leaves questions untagged, and running the
    tagger again picks up exactly those."""
    first = _Router(ok("CLO-1", 0.9), RuntimeError("x"), RuntimeError("x"), RuntimeError("x"))
    _with(monkeypatch, first)
    once, report1 = await tag_pack(_pack(_q("Q1"), _q("Q2")), max_attempts=3)
    assert (report1.tagged, report1.failed) == (1, 1)

    second = _Router(ok("CLO-2", 0.85))
    _with(monkeypatch, second)
    twice, report2 = await tag_pack(once)

    assert second.calls == 1, "the already-tagged question must not be re-asked"
    assert report2.already_tagged == 1 and report2.tagged == 1
    assert {q.question_id: q.clo_id for q in twice.questions} == {"Q1": "CLO-1", "Q2": "CLO-2"}


@pytest.mark.asyncio
async def test_tagging_returns_a_new_pack_and_leaves_the_input_alone(monkeypatch):
    """A partially-tagged run must not be mistakable for the input it came
    from."""
    _with(monkeypatch, _Router(ok()))
    original = _pack(_q("Q1"))
    tagged, _ = await tag_pack(original)

    assert original.questions[0].clo_id is None
    assert tagged.questions[0].clo_id == "CLO-1"
    assert tagged is not original


@pytest.mark.asyncio
async def test_rerunning_a_fully_tagged_pack_is_a_no_op(monkeypatch):
    router = _Router()
    _with(monkeypatch, router)
    done = _pack(_q("Q1", clo_id="CLO-1"), _q("Q2", clo_id="CLO-2"))
    tagged, report = await tag_pack(done)

    assert router.calls == 0
    assert report.as_dict()["already_tagged"] == 2
    assert [q.clo_id for q in tagged.questions] == ["CLO-1", "CLO-2"]


# --- the pure decision function -------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ('{"clo_id": "CLO-1", "confidence": 0.9}', "tagged"),
    ('```json\n{"clo_id": "CLO-1", "confidence": 0.9}\n```', "tagged"),
    ('{"clo_id": null, "confidence": 0.2}', "untagged"),
    ('{"clo_id": "CLO-9", "confidence": 0.9}', "untagged"),
    ("garbage", "untagged"),
])
def test_the_validator_is_pure_and_testable_without_a_provider(raw, expected):
    """Validation lives apart from the call precisely so it can be exercised
    exhaustively without spending a token."""
    assert _decide(raw, ALLOWED).status == expected


def test_the_report_shape_is_stable():
    """The five numbers an operator acts on, plus the two that explain them."""
    assert set(TaggingReport().as_dict()) == {
        "total", "tagged", "low_confidence", "untagged", "failed", "already_tagged"
    }


def test_provider_failure_is_a_distinct_exception():
    """A call that did not complete is not the same as a reply we chose not to
    trust — only the first is worth retrying."""
    assert issubclass(ProviderFailure, Exception)
