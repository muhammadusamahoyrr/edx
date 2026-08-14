"""The agent's tool surface. Three tools, all read-only.

Every one of them is registered at import, and every one of them reaches data
through the `CourseIntelligence` boundary — so `_authorize` (token scope + live
enrollment) and `_audit` run per call, not per turn. `.importlinter` contract 3
covers this package, so a fourth tool cannot quietly reach the store directly.

**Three, not four, and the count is a latency decision.** `get_clos` and
`get_mastery` were merged into `get_plan_context` on 2026-08-11 after profiling
showed six model round trips costing 145 s of a 222 s turn — one per tool call,
because `qwen2.5:7b` emits exactly one call per turn however it is prompted. The
tools themselves cost about a millisecond each, so the only lever is needing
fewer of them. A planner needs the outcomes and the student's history on every
single turn; making that two calls bought nothing and cost a round trip.

The general rule this leaves behind: **a tool boundary that splits two facts
always fetched together is a latency bug on any model that cannot batch.**

**The gate runs here, per tool** (decision 6). `search_course_content` applies the
same `ai.gate` the chat pipeline applies, to its own result set, with the same
threshold. A gated call returns `GATED` with no chunks — so nothing that failed
the bar is available to be cited, which is the property that makes the
"citations may only come from chunks that passed a gate" rule true by
construction rather than by prompt.

**Whole result sets pass or fail together**, exactly as they do in chat: the gate
tests the top score, and the model then sees all the retrieved chunks or none of
them. Returning only the chunks individually above tau would be a *different*
gate, tuned on nothing, and the 0-false-answer result on the gold set was measured
against this one.
"""

from __future__ import annotations

import logging

from coursemate_contracts.mastery import CLOMastery

from ..ai import gate
from ..ai.retrieval import CourseContextProvider
from ..boundary.impl import AuthorizationError, boundary
from .registry import Tool, ToolContext, ToolResult, ToolStatus, registry
from .schemas import (
    GetPlanContextArgs,
    SearchCourseContentArgs,
    SearchPastQuestionsArgs,
)

log = logging.getLogger(__name__)

_context_provider = CourseContextProvider()


# --- 1. course content, gated ---------------------------------------------


def _search_course_content(args: SearchCourseContentArgs, ctx: ToolContext) -> ToolResult:
    try:
        result = _context_provider.fetch_sync(args.query, ctx.claims, limit=args.limit)
    except AuthorizationError as exc:
        # Denied scope returns a refusal, never another cohort's content. Same
        # rule as the chat path: the student gets "not covered", not an error
        # that confirms the material exists.
        log.warning("agent retrieval denied: %s", exc)
        return ToolResult(
            tool="search_course_content",
            status=ToolStatus.GATED,
            data={"chunks": []},
            message="Not available for this student's enrollment.",
            gate_applied=True,
        )

    outcome = gate.evaluate(result)
    if outcome is gate.GateOutcome.NO_INDEX:
        return ToolResult(
            tool="search_course_content",
            status=ToolStatus.GATED,
            data={"chunks": []},
            message="This course has not been indexed yet. Say it is still being prepared.",
            gate_applied=True,
        )
    if outcome is gate.GateOutcome.BELOW_THRESHOLD:
        return ToolResult(
            tool="search_course_content",
            status=ToolStatus.GATED,
            data={"chunks": []},
            message=(
                "No course material meets the confidence bar for this query. "
                "Rephrasing may help once; if it does not, this is not covered."
            ),
            gate_applied=True,
        )

    return ToolResult(
        tool="search_course_content",
        status=ToolStatus.OK,
        data={
            "chunks": [
                {
                    "label": i,
                    "display_name": c.citation.display_name,
                    "usage_key": c.citation.usage_key,
                    "text": c.text,
                }
                for i, c in enumerate(result.chunks, start=1)
            ]
        },
    )


# --- 2. past questions ------------------------------------------------------


def _search_past_questions(args: SearchPastQuestionsArgs, ctx: ToolContext) -> ToolResult:
    offering_id = ctx.claims.offering_id
    if not boundary.has_exam_pack(offering_id):
        return ToolResult(
            tool="search_past_questions",
            status=ToolStatus.GATED,
            data={"questions": []},
            message="No past papers have been loaded for this course.",
        )

    try:
        rows = boundary.search_past_questions(
            offering_id,
            ctx.claims,
            query=args.query,
            clo_id=args.clo_id,
            exam_type=args.exam_type,
            # Model-facing names differ from the internal ones on purpose; the
            # mapping lives here and nowhere else. See SearchPastQuestionsArgs.
            year_from=args.earliest_year,
            min_marks=args.minimum_marks,
            limit=args.limit,
        )
    except AuthorizationError as exc:
        log.warning("agent past-question search denied: %s", exc)
        return ToolResult(
            tool="search_past_questions",
            status=ToolStatus.GATED,
            data={"questions": []},
            message="Not available for this student's enrollment.",
        )

    # An empty result here is OK, not GATED and not an error: the filter was
    # valid and the honest answer is "no question matches those constraints".
    # The model is told which constraints to relax rather than left to guess
    # whether the tool is broken.
    return ToolResult(
        tool="search_past_questions",
        status=ToolStatus.OK,
        data={
            "questions": [
                {
                    "question_id": q.question_id,
                    "text": q.text,
                    "clo_id": q.clo_id,
                    "marks": q.marks,
                    "year": q.year,
                    "exam_type": q.exam_type,
                    "source": q.source_doc_id,
                    "page": q.page,
                    # Carried so the answer can say so. §7.6 requires a derived
                    # difficulty to be labelled derived wherever it is shown, and
                    # a field the model never sees cannot be labelled.
                    "difficulty": q.difficulty,
                    "difficulty_is_derived": q.difficulty_is_derived,
                    "low_confidence": q.low_confidence_flag,
                }
                for q in rows
            ],
            "filters_applied": args.model_dump(exclude_none=True),
        },
        message="" if rows else "No question matches those filters. Try relaxing one.",
    )


# --- 3. plan context: outcomes + mastery + what is searchable ----------------


def _get_plan_context(args: GetPlanContextArgs, ctx: ToolContext) -> ToolResult:
    """One call for everything a revision plan always needs.

    Replaces `get_clos` and `get_mastery`. See `GetPlanContextArgs` for the
    measurement that motivated the merge; the short version is that a round trip
    costs ~26 s on the local model and the tools underneath cost ~1 ms, so the
    number of calls is the only thing worth optimising.

    Reads exactly what the two tools read, through the same boundary, with the
    same authorization. Nothing became more permissive: `get_clos` still goes
    through `_authorize`, and mastery still comes from the request-scoped
    snapshot rather than any store.
    """
    offering_id = ctx.claims.offering_id

    try:
        clos = boundary.get_clos(offering_id, ctx.claims)
    except AuthorizationError as exc:
        log.warning("agent plan-context read denied: %s", exc)
        return ToolResult(
            tool="get_plan_context",
            status=ToolStatus.GATED,
            data={"clos": [], "mastery": []},
            message="Not available for this student's enrollment.",
        )

    snapshot = ctx.mastery
    known = snapshot is not None and snapshot.offering_id == offering_id
    if snapshot is not None and not known:
        # Browser-carried and therefore attacker-controlled. A snapshot minted for
        # another offering shapes nothing here.
        log.warning(
            "mastery snapshot for %s arrived on a request scoped to %s; ignoring",
            snapshot.offering_id, offering_id,
        )
    entries: list[CLOMastery] = list(snapshot.clos) if known else []

    return ToolResult(
        tool="get_plan_context",
        status=ToolStatus.OK,
        data={
            "clos": [
                {
                    "clo_id": c.clo_id,
                    "text": c.text,
                    # §7.3: CLO extraction is assisted, never asserted. An
                    # unconfirmed list is usable but must not be presented as the
                    # instructor's, so the flag travels with the data.
                    "confirmed": bool(c.confirmed_by),
                }
                for c in clos
            ],
            "mastery": [
                {
                    "clo_id": c.clo_id,
                    "attempts": c.attempts,
                    "correct": c.correct,
                    # None, not 0.0, when untried — an unattempted outcome is
                    # unknown, and ranking it level with one failed six times
                    # would invert the recommendation it exists to inform.
                    "accuracy": c.accuracy,
                }
                for c in entries
            ],
            #: False means "no history yet", never "the lookup broke". A new
            #: student has exactly this, and the runner must not read it as a
            #: failure.
            "mastery_known": known,
            "mastery_truncated": bool(known and snapshot.truncated),
            #: Not part of the merge — part of the same reasoning. Telling the
            #: planner up front that there is nothing to search saves an entire
            #: wasted round trip, which on this hardware is ~26 seconds.
            "past_papers_available": boundary.has_exam_pack(offering_id),
        },
        message="" if clos else "No learning outcomes are defined for this course.",
    )


registry.register(Tool(
    name="search_course_content",
    description=(
        "Search this course's published material. Returns passages with labels "
        "you must cite. Returns nothing when no passage meets the confidence bar "
        "— that means the course does not cover it, not that the search failed."
    ),
    args_model=SearchCourseContentArgs,
    handler=_search_course_content,
))

registry.register(Tool(
    name="search_past_questions",
    description=(
        "Find real past-paper questions by structured filter. Prefer these "
        "filters over free text — 'final papers from 2023 worth 10+ marks' is a "
        "filter, not a search. The parameters are exactly: clo_id (one outcome "
        "id or a list of them), exam_type, earliest_year, minimum_marks, query, "
        "limit. Use no other parameter name."
    ),
    args_model=SearchPastQuestionsArgs,
    handler=_search_past_questions,
))

registry.register(Tool(
    name="get_plan_context",
    description=(
        "Everything needed to plan revision, in one call: this course's learning "
        "outcomes, this student's practice history for each of them, and whether "
        "past papers exist to search. Call this FIRST and only once — it already "
        "contains the outcomes and the history, so nothing further is needed to "
        "rank what to revise."
    ),
    args_model=GetPlanContextArgs,
    handler=_get_plan_context,
))
