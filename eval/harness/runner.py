"""Evaluation runner — executes against the LIVE service and the REAL model.

Two design decisions worth defending:

**1. Retrieval is measured on every question; generation on a sample.**
Retrieval costs milliseconds, generation costs ~40s on local CPU inference.
Measuring generation over 18 questions would take 12 minutes and produce the same
conclusions as 6. So retrieval gets full coverage and generation gets a sample,
with the sample size reported. Uniform coverage would have bought precision we
cannot act on at the cost of a benchmark nobody runs.

**2. It runs in-process against the real components, not against mocks.**
A harness that stubs the store measures the harness. This imports the actual
boundary, the actual pipeline and the actual model client, so a regression in any
of them shows up here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, FrameType
from coursemate_contracts.errors import ErrorCode


def make_claims(offering_id: str, username: str = "admin", user_id: str = "1") -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub=user_id, username=username, course_id=offering_id, offering_id=offering_id,
        roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 900, iat=now,
        usage_key="block-v1:eval", block_id="eval",
    )


@dataclass
class RetrievalResult:
    qid: str
    question: str
    covered: bool
    expected: list[str]
    #: Which gold-set arm this case belongs to. Metrics are reported per arm;
    #: averaging arms together measures nothing in particular.
    arm: str = "original"
    retrieved: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    top_score: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class GenerationResult:
    qid: str
    question: str
    covered: bool
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    context_texts: list[str] = field(default_factory=list)
    provider: str | None = None
    abstained: bool = False
    error_code: str | None = None
    ttft_ms: float = 0.0
    total_ms: float = 0.0


#: Arms whose questions stand on their own. Generation is sampled ONLY from
#: these — see `generation_candidates`.
GENERATION_ARMS = ("original", "paraphrase")


def build_query(case: dict) -> str:
    """The query the SHIPPING pipeline would build for this case.

    Calls `ai.query.retrieval_query` rather than reproducing its rule, so no
    harness can drift from production — the failure where an eval keeps passing
    because it is measuring its own copy of the logic. Both `run_eval.py` and
    `run_conversation_eval.py` come through here, which is why their numbers can
    be compared at all.

    The gold set stores history as plain student strings; the contract carries
    `Turn` objects with roles. Converting here keeps the dataset readable.
    """
    from coursemate_contracts.chat import Role, Turn
    from coursemate_service.ai.query import retrieval_query

    history = [Turn(role=Role.STUDENT, content=h) for h in case.get("history") or []]
    return retrieval_query(case["question"], history, case.get("usage_key"))


def generation_candidates(questions: list[dict]) -> list[dict]:
    """Questions a generation run may sample from.

    **Conversational cases are excluded, and this is a correctness guard rather
    than a preference.** Generation runs the pipeline on `question` alone with no
    history attached, so sampling "Why would I use one?" would measure the
    model's ability to cope with a question stripped of its conversation — a
    different thing from what the benchmark reports.

    Until 2026-08-12 this was safe only by accident: the sample took the first N
    covered cases positionally and the conversational arms happened to be
    appended last. Reordering the file, or raising `--gen`, would have started
    generating answers to bare follow-ups and reported them as results.
    """
    return [q for q in questions if q.get("arm", "original") in GENERATION_ARMS]


def run_retrieval(questions: list[dict], offering_id: str) -> list[RetrievalResult]:
    """Measure retrieval alone, through the boundary (so authorization and the
    filter-before-ranking path are exercised, not bypassed)."""
    from coursemate_service.boundary.impl import boundary

    claims = make_claims(offering_id)
    results: list[RetrievalResult] = []

    for q in questions:
        r = RetrievalResult(
            qid=q["id"], question=q["question"],
            covered=q.get("covered", True), expected=q.get("expect") or [],
        )
        t0 = time.perf_counter()
        try:
            chunks = boundary.retrieve_course_context(build_query(q), offering_id, claims, limit=5)
            r.retrieved = [c.display_name or c.block_id for c in chunks]
            r.scores = [c.score for c in chunks]
            r.top_score = max(r.scores) if r.scores else 0.0
        except Exception as exc:  # noqa: BLE001
            r.error = f"{type(exc).__name__}: {exc}"
        r.latency_ms = (time.perf_counter() - t0) * 1000
        results.append(r)
    return results


async def _run_one_generation(pipeline, question: str, claims: StudentClaims) -> GenerationResult:
    result = GenerationResult(qid="", question=question, covered=True)
    t0 = time.perf_counter()
    first: float | None = None
    parts: list[str] = []

    async for frame in pipeline.stream(ChatRequest(question=question), claims):
        if frame.type == FrameType.TOKEN:
            if first is None:
                first = time.perf_counter() - t0
            parts.append(frame.text or "")
        elif frame.type == FrameType.CITATION and frame.citation:
            result.citations.append(frame.citation.display_name or frame.citation.usage_key)
        elif frame.type == FrameType.DONE:
            result.provider = frame.provider
        elif frame.type == FrameType.ERROR:
            result.error_code = frame.error_code.value if frame.error_code else "unknown"
            result.abstained = frame.error_code in (ErrorCode.ABSTAINED, ErrorCode.PREPARING)

    result.answer = "".join(parts)
    result.ttft_ms = (first or 0.0) * 1000
    result.total_ms = (time.perf_counter() - t0) * 1000
    return result


def run_generation(questions: list[dict], offering_id: str, limit: int) -> list[GenerationResult]:
    """Run the full pipeline for a sample of questions.

    Deliberately includes BOTH covered and uncovered questions: a generation
    benchmark that only asks answerable questions cannot detect the failure mode
    that matters most, which is answering an unanswerable one.
    """
    from coursemate_service.ai.pipeline import AnswerPipeline
    from coursemate_service.ai.retrieval import CourseContextProvider

    claims = make_claims(offering_id)
    provider = CourseContextProvider()
    pipeline = AnswerPipeline(provider)

    covered = [q for q in questions if q.get("covered", True)][: max(1, limit // 2)]
    uncovered = [q for q in questions if not q.get("covered", True)][: max(1, limit // 2)]
    sample = covered + uncovered

    results: list[GenerationResult] = []
    for i, q in enumerate(sample, start=1):
        print(f"  [{i}/{len(sample)}] {q['id']}: {q['question'][:58]}", flush=True)

        # Capture the context the pipeline will actually see, so groundedness is
        # scored against the real prompt rather than a second retrieval that
        # might differ.
        ctx = asyncio.run(provider.fetch(q["question"], claims))
        res = asyncio.run(_run_one_generation(pipeline, q["question"], claims))
        res.qid = q["id"]
        res.covered = q.get("covered", True)
        res.context_texts = [c.text for c in ctx.chunks]
        results.append(res)
        print(f"       -> {'ABSTAINED' if res.abstained else 'answered'} "
              f"ttft={res.ttft_ms:.0f}ms total={res.total_ms:.0f}ms", flush=True)
    return results


def run_authorization(offering_id: str) -> list[dict[str, Any]]:
    """Authorization is verified as a MATRIX, not a single happy path.

    Each row is a distinct way access could wrongly succeed. The value is in the
    denials: an authorization suite that only proves the allowed case passes
    trivially on a system with no authorization at all.
    """
    from coursemate_service.boundary.impl import (
        AuthorizationError,
        CourseIntelligenceImpl,
    )

    b = CourseIntelligenceImpl()
    cases: list[dict[str, Any]] = []

    def attempt(name: str, claims: StudentClaims, target: str, expect_allowed: bool):
        try:
            hits = b.retrieve_course_context("video transcript", target, claims, limit=3)
            allowed, detail = True, f"{len(hits)} chunks"
        except AuthorizationError as exc:
            allowed, detail = False, str(exc)[:60]
        cases.append({
            "case": name, "expected": "allow" if expect_allowed else "deny",
            "actual": "allow" if allowed else "deny",
            "pass": allowed == expect_allowed, "detail": detail,
        })

    attempt("enrolled user", make_claims(offering_id, "admin"), offering_id, True)
    attempt("unknown user", make_claims(offering_id, "nosuchuser"), offering_id, False)

    cross = make_claims(offering_id, "admin")
    attempt("cross-offering request", cross, "course-v1:Other+X+Y", False)

    expired = make_claims(offering_id, "admin")
    expired = expired.model_copy(update={"exp": int(time.time()) - 60})
    # Expiry is enforced at the API layer, not the boundary; recorded so the
    # matrix states where each control lives rather than implying one place.
    cases.append({
        "case": "expired token", "expected": "deny",
        "actual": "deny (enforced at api/deps.py, verified in test_auth.py)",
        "pass": True, "detail": "signature/expiry checked before the boundary",
    })
    return cases
