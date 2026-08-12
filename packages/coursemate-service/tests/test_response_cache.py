"""The first-turn response cache, and the isolation it must never lose.

§10.2 says caching is how isolation quietly fails *after* every filter is written
correctly, and the `knowledge/cache/README.md` in this repo says the same thing
in more detail — the rules were written down and tested before any cache existed
so that whoever wired one inherited them. This file is where they stop being
vacuous.

The tests that look paranoid are defending one sentence: two callers who may see
different content must never collide on one key. Everything else here is
ordinary caching behaviour.
"""

from __future__ import annotations

import json
import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, Citation, FrameType, Mode, Role, Turn
from coursemate_contracts.errors import ErrorCode
from coursemate_service import response_cache as RC
from coursemate_service import shared_state
from coursemate_service.ai import client as ai_client
from coursemate_service.ai import pipeline as pl
from coursemate_service.ai.context import ContextChunk, ContextResult

OFFERING = "course-v1:X+Y+Z"
OTHER_OFFERING = "course-v1:X+Y+OTHER"
VERSION = "idx-v1"


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    def get(self, k):
        if self.fail:
            raise ConnectionError("down")
        return self.kv.get(k)

    def setex(self, k, ttl, v):
        if self.fail:
            raise ConnectionError("down")
        self.kv[k] = v
        self.ttl[k] = ttl


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    shared_state.reset_for_tests()
    monkeypatch.setattr(RC.settings, "response_cache_enabled", True)
    # Budget out of the way: C1 is tested in its own file and a ceiling hit here
    # would mask a cache result.
    monkeypatch.setattr(pl.settings, "require_grounding", True)
    yield
    shared_state.reset_for_tests()


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(shared_state, "get_redis", lambda: fake)
    return fake


def _claims(sub="u1", offering=OFFERING, groups=(), roles=("student",)) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=sub, course_id=offering, offering_id=offering, roles=list(roles),
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now, group_tokens=list(groups),
    )


# ============================================================ the key alone ==
#
# Keyed directly, without the pipeline, so a collision is attributable to the
# key rather than to something upstream having refused the request for an
# unrelated reason.


def key(request=None, claims=None, version=VERSION):
    return RC.cache_key(request or ChatRequest(question="What is a cohort?"),
                        claims or _claims(), version)


def test_the_same_question_from_the_same_caller_is_one_key():
    assert key() == key()


def test_whitespace_and_case_do_not_make_a_second_key():
    a = key(ChatRequest(question="What is a cohort?"))
    b = key(ChatRequest(question="  what  IS a COHORT? "))
    assert a == b


def test_two_students_do_not_share_a_key():
    assert key(claims=_claims(sub="u1")) != key(claims=_claims(sub="u2"))


def test_two_offerings_do_not_share_a_key():
    assert key(claims=_claims(offering=OFFERING)) != key(
        claims=_claims(offering=OTHER_OFFERING)
    )


def test_two_group_scopes_do_not_share_a_key():
    """The block-level access filter runs on `group_tokens`, so two callers with
    different tokens are retrieving from different candidate sets. Colliding
    here would serve restricted content to someone the filter excluded."""
    assert key(claims=_claims(groups=["cohort:a"])) != key(
        claims=_claims(groups=["cohort:b"])
    )
    assert key(claims=_claims(groups=[])) != key(claims=_claims(groups=["cohort:a"]))


def test_group_token_order_does_not_produce_two_keys():
    """A cache miss is cheap; the inverse bug is the leak. Sorting keeps the
    mapping one-way."""
    assert key(claims=_claims(groups=["a:1", "b:2"])) == key(
        claims=_claims(groups=["b:2", "a:1"])
    )


def test_a_role_difference_does_not_share_a_key():
    """Staff retrieve from a wider candidate set. This is the v4 bug the design
    calls out by name."""
    assert key(claims=_claims(roles=["student"])) != key(
        claims=_claims(roles=["student", "staff"])
    )


def test_a_new_index_version_is_a_new_key():
    assert key(version="idx-v1") != key(version="idx-v2")


def test_mode_is_part_of_the_key():
    """Socratic returns a guiding question, not an answer. Same words in, very
    different thing out."""
    a = key(ChatRequest(question="q?", mode=Mode.DIRECT))
    b = key(ChatRequest(question="q?", mode=Mode.SOCRATIC))
    assert a != b


def test_usage_key_is_part_of_the_key_before_anything_reads_it():
    """B1/B2 accept `usage_key` and deliberately ignore it, so it changes no
    answer today. Including it now means the day B3 wires it, the cache cannot
    serve a block's answer to a student standing on a different one."""
    a = key(ChatRequest(question="q?", usage_key=None))
    b = key(ChatRequest(question="q?", usage_key="block-v1:a"))
    assert a != b


# ---------------------------------------------------------- first-turn guard --
#
# `not request.history` was the original rule. It passed every test here and was
# unreachable in production: `tutor.js` pushes the question into `history` before
# building the request, so the browser never sends an empty one — not even on a
# student's first question in a block. Live verification found it after a full
# 50-second generation left `resp:*` at zero.
#
# The captured payload below is the fix's real specification. The synthetic cases
# around it are there to show the normalisation does not overreach.

#: Copied verbatim off the wire during C2 live verification, 2026-08-12, from a
#: block whose history had just been cleared — a genuine first question. Kept as
#: raw JSON rather than reconstructed with `Turn(...)`, because reconstructing it
#: is exactly how the bug survived: a hand-built fixture encodes what the
#: contract says, and the contract is not what the client sends.
BROWSER_FIRST_TURN_PAYLOAD = json.loads(
    '{"question":"What is a cohort?",'
    '"history":[{"role":"student","content":"What is a cohort?"}],'
    '"mode":"direct"}'
)


def test_a_truly_empty_history_is_first_turn():
    assert RC.is_cacheable_request(ChatRequest(question="q?")) is True


def test_the_verbatim_browser_payload_is_first_turn():
    """The regression. This exact body produced a 50-second generation and zero
    cache entries in production."""
    request = ChatRequest(**BROWSER_FIRST_TURN_PAYLOAD)
    assert request.history, "precondition: the browser really does send history"
    assert len(request.history) == 1
    assert request.history[0].content == request.question
    assert RC.is_cacheable_request(request) is True


def test_the_browser_shape_is_first_turn():
    assert RC.is_cacheable_request(ChatRequest(
        question="What is a cohort?",
        history=[Turn(role=Role.STUDENT, content="What is a cohort?")],
    )) is True


def test_a_genuine_prior_conversation_is_not_first_turn():
    assert RC.is_cacheable_request(ChatRequest(
        question="Why would I use one?",
        history=[Turn(role=Role.STUDENT, content="What is a cohort?")],
    )) is False


def test_the_echo_alongside_a_real_prior_turn_is_not_first_turn():
    """The overreach case. Stripping the echo must not strip the conversation
    underneath it — this is a follow-up and its answer depends on the first
    turn, so caching it would serve one student's conversation to another."""
    assert RC.is_cacheable_request(ChatRequest(
        question="Why would I use one?",
        history=[
            Turn(role=Role.STUDENT, content="What is a cohort?"),
            Turn(role=Role.TUTOR, content="A cohort is a group of learners."),
            Turn(role=Role.STUDENT, content="Why would I use one?"),
        ],
    )) is False


def test_a_tutor_turn_repeating_the_question_is_not_stripped():
    """Only a STUDENT turn is the echo. A tutor turn is conversation the model
    produced, and its presence means this is not a first turn however it reads."""
    assert RC.is_cacheable_request(ChatRequest(
        question="What is a cohort?",
        history=[Turn(role=Role.TUTOR, content="What is a cohort?")],
    )) is False


def test_a_different_question_in_history_is_not_stripped():
    assert RC.is_cacheable_request(ChatRequest(
        question="What is a cohort?",
        history=[Turn(role=Role.STUDENT, content="What is a content group?")],
    )) is False


def test_the_echo_is_matched_after_trimming_whitespace():
    assert RC.is_cacheable_request(ChatRequest(
        question="What is a cohort?",
        history=[Turn(role=Role.STUDENT, content="  What is a cohort?  ")],
    )) is True


# ======================================================== store and retrieve ==


def test_a_written_payload_reads_back(redis):
    assert RC.write("k", {"kind": "answer", "answer": "hello"}, []) is True
    assert RC.read("k") == {"kind": "answer", "answer": "hello", "v": RC.PAYLOAD_VERSION}


def test_a_write_sets_the_ttl(redis):
    RC.write("k", {"kind": "answer"}, [])
    assert redis.ttl["k"] == RC.settings.response_cache_ttl_seconds


def test_an_entry_from_an_older_payload_shape_is_a_miss(redis):
    redis.kv["k"] = json.dumps({"kind": "answer", "answer": "x", "v": RC.PAYLOAD_VERSION - 1})
    assert RC.read("k") is None


def test_corrupt_json_is_a_miss_not_a_crash(redis):
    redis.kv["k"] = "{not json"
    assert RC.read("k") is None


def test_a_personal_chunk_is_never_stored(redis):
    """§6.4/§10.2: not stored, not served. `assert_cacheable` raises; `write`
    catches it, logs, and returns False — the student's answer has already
    streamed and must not fail because bookkeeping refused."""
    personal = ContextChunk(
        text="t", citation=Citation(usage_key="u"), score=0.9, is_personal=True
    )
    assert RC.write("k", {"kind": "answer"}, [personal]) is False
    assert RC.read("k") is None


def test_the_kill_switch_disables_both_ends(redis, monkeypatch):
    monkeypatch.setattr(RC.settings, "response_cache_enabled", False)
    assert RC.write("k", {"kind": "answer"}, []) is False
    redis.kv["k"] = json.dumps({"kind": "answer", "v": RC.PAYLOAD_VERSION})
    assert RC.read("k") is None


# ============================================================ redis is down ==


def test_no_redis_means_every_read_misses(monkeypatch):
    monkeypatch.setattr(shared_state, "get_redis", lambda: None)
    assert RC.read("k") is None
    assert RC.write("k", {"kind": "answer"}, []) is False


def test_a_failing_redis_never_raises(redis):
    """A cache is an optimisation. `shared_state` already argues that refusing to
    serve when one is down turns it into an outage — so both ends degrade to
    'no cache' and neither propagates."""
    redis.fail = True
    assert RC.read("k") is None
    assert RC.write("k", {"kind": "answer"}, []) is False


# ============================================================== the pipeline ==


class _Ctx:
    """Retrieval stub. Counts fetches so a cache hit is distinguishable from a
    second retrieval."""

    def __init__(self, score=0.9, version=VERSION, personal=False):
        self.score, self.version, self.personal = score, version, personal
        self.calls = 0

    async def fetch(self, question, claims):
        self.calls += 1
        return ContextResult(
            chunks=[ContextChunk(
                text="A cohort is a group of learners.",
                citation=Citation(usage_key="u1", display_name="Cohorts"),
                score=self.score, is_personal=self.personal,
            )],
            top_score=self.score,
            index_version=self.version,
        )


class _Chunk:
    def __init__(self, text, finish=None):
        self.choices = [type("C", (), {
            "delta": type("D", (), {"content": text})(),
            "finish_reason": finish,
        })()]
        self.model = "test-model"


def _router(calls, text="A cohort is a group of learners."):
    async def _stream():
        yield _Chunk(text, "stop")

    class _Router:
        async def acompletion(self, **kw):
            calls.append(kw)
            return _stream()

    return _Router()


def _install(monkeypatch):
    calls: list = []
    ai_client.reset_router()
    monkeypatch.setattr(pl, "get_router", lambda: _router(calls))
    monkeypatch.setattr(pl, "deployment_of", lambda part: pl.PRIMARY_DEPLOYMENT)
    monkeypatch.setattr(pl.settings, "student_daily_token_budget", 0)
    return calls


async def _ask(ctx, request, claims):
    return [f async for f in pl.AnswerPipeline(ctx).stream(request, claims)]


@pytest.mark.asyncio
async def test_the_second_identical_question_is_served_from_cache(redis, monkeypatch):
    calls = _install(monkeypatch)
    ctx = _Ctx()

    first = await _ask(ctx, ChatRequest(question="What is a cohort?"), _claims())
    second = await _ask(ctx, ChatRequest(question="What is a cohort?"), _claims())

    assert len(calls) == 1, "the provider was called again on a cache hit"
    assert ctx.calls == 2, "retrieval must still run — it is what enforces authz"

    text = lambda fs: "".join(f.text or "" for f in fs if f.type == FrameType.TOKEN)
    assert text(second) == text(first)
    assert [f.citation.usage_key for f in second if f.type == FrameType.CITATION] == \
           [f.citation.usage_key for f in first if f.type == FrameType.CITATION]
    assert second[-1].type == FrameType.DONE


@pytest.mark.asyncio
async def test_the_browser_payload_reaches_the_cache_end_to_end(redis, monkeypatch):
    """The whole defect, end to end, driven by the body captured off the wire.

    Before the fix this generated twice and stored nothing — which is precisely
    what production did. Asserting on the pipeline rather than only on
    `is_cacheable_request` is deliberate: the unit-level guard passed for the
    broken version too, because it was called with a fixture nobody had checked
    against a real client.
    """
    calls = _install(monkeypatch)
    ctx = _Ctx()
    request = ChatRequest(**BROWSER_FIRST_TURN_PAYLOAD)

    first = await _ask(ctx, request, _claims())
    assert redis.kv, "the browser's first turn was not cached"

    second = await _ask(ctx, request, _claims())

    assert len(calls) == 1, "the provider was called again for a browser first turn"

    def text(fs):
        return "".join(f.text or "" for f in fs if f.type == FrameType.TOKEN)

    assert text(second) == text(first)
    assert second[-1].type == FrameType.DONE


@pytest.mark.asyncio
async def test_a_browser_follow_up_still_misses(redis, monkeypatch):
    """The other side of the same change. The browser sends the echo on EVERY
    turn, so if the normalisation were positional rather than content-matched,
    every follow-up would look like a first turn and the cache would start
    serving conversations."""
    calls = _install(monkeypatch)
    ctx = _Ctx()
    follow_up = ChatRequest(
        question="Why would I use one?",
        history=[
            Turn(role=Role.STUDENT, content="What is a cohort?"),
            Turn(role=Role.TUTOR, content="A cohort is a group of learners."),
            Turn(role=Role.STUDENT, content="Why would I use one?"),
        ],
    )

    await _ask(ctx, follow_up, _claims())
    await _ask(ctx, follow_up, _claims())

    assert len(calls) == 2, "a follow-up was served from the first-turn cache"
    assert redis.kv == {}, "a follow-up was written to the first-turn cache"


@pytest.mark.asyncio
async def test_the_provider_is_not_called_on_a_hit(redis, monkeypatch):
    """Stated separately from the frame comparison because it is the only reason
    the cache exists: a hit that still paid for generation is a slower miss."""
    calls = _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims())
    calls.clear()
    await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert calls == []


@pytest.mark.asyncio
async def test_a_multi_turn_request_never_touches_the_cache(redis, monkeypatch):
    """B1/B2 make the retrieval query depend on the previous turn, so a follow-up
    has no stable answer to cache. Two students asking 'why?' after different
    first turns must not collide."""
    calls = _install(monkeypatch)
    ctx = _Ctx()
    req = ChatRequest(
        question="Why would I use one?",
        history=[Turn(role=Role.STUDENT, content="What is a cohort?")],
    )

    await _ask(ctx, req, _claims())
    await _ask(ctx, req, _claims())

    assert len(calls) == 2, "a follow-up was served from cache"
    assert redis.kv == {}, "a follow-up was written to the cache"


@pytest.mark.asyncio
async def test_a_second_student_does_not_get_the_first_students_answer(redis, monkeypatch):
    calls = _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims(sub="u1"))
    await _ask(ctx, ChatRequest(question="q?"), _claims(sub="u2"))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_second_offering_does_not_get_the_first_offerings_answer(redis, monkeypatch):
    calls = _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims(offering=OFFERING))
    await _ask(ctx, ChatRequest(question="q?"), _claims(offering=OTHER_OFFERING))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_different_group_scope_does_not_get_the_cached_answer(redis, monkeypatch):
    """The one that matters most. Group tokens decide which blocks retrieval may
    see, so sharing across them is how restricted content escapes."""
    calls = _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims(sub="u1", groups=["cohort:paid"]))
    await _ask(ctx, ChatRequest(question="q?"), _claims(sub="u1", groups=["cohort:free"]))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_reindex_invalidates_the_cached_answer(redis, monkeypatch):
    calls = _install(monkeypatch)
    ctx = _Ctx(version="idx-v1")
    await _ask(ctx, ChatRequest(question="q?"), _claims())
    ctx.version = "idx-v2"
    await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert len(calls) == 2, "a stale answer survived a reindex"


@pytest.mark.asyncio
async def test_an_abstention_is_cached_and_replayed(redis, monkeypatch):
    calls = _install(monkeypatch)
    monkeypatch.setattr(pl.settings, "confidence_threshold", 0.95)
    ctx = _Ctx(score=0.10)

    first = await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert first[-1].error_code == ErrorCode.ABSTAINED
    assert redis.kv, "the abstention was not stored"

    second = await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert second[-1].error_code == ErrorCode.ABSTAINED
    assert calls == [], "an abstention should never reach the provider"


@pytest.mark.asyncio
async def test_preparing_is_not_cached(redis, monkeypatch):
    """'Still being prepared' says the index is about to change. Caching a claim
    whose content is 'this is temporary' guarantees a wrong entry."""
    _install(monkeypatch)

    class _NoIndex:
        calls = 0

        async def fetch(self, question, claims):
            return ContextResult(chunks=[], top_score=0.0, index_missing=True)

    frames = await _ask(_NoIndex(), ChatRequest(question="q?"), _claims())
    assert frames[-1].error_code == ErrorCode.PREPARING
    assert redis.kv == {}


@pytest.mark.asyncio
async def test_an_unauthorized_caller_never_reaches_the_cache(redis, monkeypatch):
    """Authorization is enforced BEFORE any cache involvement, structurally.

    A denied caller's retrieval returns `index_version=None` (see
    `ai/retrieval.py`), and the pipeline only builds a key when it has a version.
    So there is no ordering in which a cache hit can precede the enrollment
    check — the check is not repeated in the cache because a denied caller never
    gets that far.
    """
    calls = _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert redis.kv, "precondition: something was cached for the allowed caller"
    cached_before = dict(redis.kv)

    class _Denied:
        """What `fetch_sync` returns after AuthorizationError."""

        async def fetch(self, question, claims):
            return ContextResult(chunks=[], top_score=0.0, index_missing=False,
                                 index_version=None)

    calls.clear()
    frames = await _ask(_Denied(), ChatRequest(question="q?"), _claims())

    assert frames[-1].error_code == ErrorCode.ABSTAINED, "denied scope must abstain"
    assert not [f for f in frames if f.type == FrameType.TOKEN], "cached text was served"
    assert redis.kv == cached_before, "a denied caller wrote to the cache"


@pytest.mark.asyncio
async def test_a_redis_outage_does_not_break_the_tutor(redis, monkeypatch):
    calls = _install(monkeypatch)
    redis.fail = True
    ctx = _Ctx()

    frames = await _ask(ctx, ChatRequest(question="q?"), _claims())

    assert frames[-1].type == FrameType.DONE
    assert "".join(f.text or "" for f in frames if f.type == FrameType.TOKEN)
    assert len(calls) == 1, "the answer was still generated normally"


@pytest.mark.asyncio
async def test_the_cached_payload_preserves_citations_and_done_metadata(redis, monkeypatch):
    _install(monkeypatch)
    ctx = _Ctx()
    await _ask(ctx, ChatRequest(question="q?"), _claims())

    stored = json.loads(next(iter(redis.kv.values())))
    assert stored["kind"] == "answer"
    assert stored["answer"]
    assert stored["citations"] and stored["citations"][0]["usage_key"] == "u1"
    assert stored["citations"][0]["display_name"] == "Cohorts"
    assert stored["truncated"] is False
    assert "provider" in stored


@pytest.mark.asyncio
async def test_a_personal_retrieval_is_not_cached_through_the_pipeline(redis, monkeypatch):
    """The control wired end to end, not just unit-tested on `write`."""
    calls = _install(monkeypatch)
    ctx = _Ctx(personal=True)

    await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert redis.kv == {}, "a personal-namespace answer was cached"

    await _ask(ctx, ChatRequest(question="q?"), _claims())
    assert len(calls) == 2, "a personal-namespace answer was served from cache"


@pytest.mark.asyncio
async def test_a_degraded_answer_is_not_cached(redis, monkeypatch):
    """It came from the fallback during an outage. Caching it freezes the
    outage's quality in for the TTL and replays a DEGRADED frame about an outage
    that has ended."""
    _install(monkeypatch)
    monkeypatch.setattr(pl, "deployment_of", lambda part: "fallback")

    frames = await _ask(_Ctx(), ChatRequest(question="q?"), _claims())

    assert any(f.type == FrameType.DEGRADED for f in frames)
    assert redis.kv == {}
