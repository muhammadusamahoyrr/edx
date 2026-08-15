"""Exam prep — design §7.

The structured question record (§7.6) is the point of the feature. Extracting only
question *text* throws away the structure the whole thing runs on: "practice for
CLO-3, from final papers, 2023 onward, worth 10+ marks" is a metadata filter over
records, not a semantic search over blobs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .mastery import MasterySnapshot


class ExamType(StrEnum):
    MID = "mid"
    FINAL = "final"
    QUIZ = "quiz"
    ASSIGNMENT = "assignment"


class ExtractionMethod(StrEnum):
    DIGITAL = "digital"
    OCR = "ocr"
    VLM = "vlm"


class QuestionRecord(BaseModel):
    """One past-paper question. A record, not a blob (§7.6)."""

    question_id: str
    tenant: str
    offering_id: str

    # --- provenance: every generated item traces to a real paper --------------
    source_doc_id: str
    page: int | None = None
    question_number: str | None = None

    text: str

    #: "Only the last 3 years" — filters out a syllabus that has since changed.
    year: int | None = None
    #: A finals plan weights `final` papers.
    exam_type: ExamType | None = None
    #: Printed on the question. Proxy for depth and time; drives plan realism and
    #: lets a study session be sized by marks budget rather than question count.
    marks: int | None = Field(default=None, ge=0)

    #: DERIVED, never printed on the paper — from marks + command verb + a model
    #: estimate. Always labelled derived and correctable (§7.6, §7.9).
    difficulty: float | None = Field(default=None, ge=0.0, le=1.0)
    difficulty_is_derived: bool = True

    topic: str | None = None
    #: AI-proposed, correctable by instructor or student (§7.5).
    clo_id: str | None = None

    #: How sure the EXTRACTOR was that this is a whole, correctly-bounded
    #: question. About the parse, never about the question's quality.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_method: ExtractionMethod | None = None

    #: How sure the offline tagger was about `clo_id`. A SEPARATE field, because
    #: overwriting `confidence` would silently destroy the extractor's signal —
    #: two different questions ("did we read it right?" and "did we file it
    #: right?") that a single number cannot answer.
    #:
    #: `None` when untagged, which includes every question the tagger refused.
    clo_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    #: Shown as uncertain for ANY reason — a shaky parse or a shaky tag. One
    #: flag because the consumer's question is "should I trust this item?", and
    #: the two confidences above are there when someone needs to know which half
    #: was weak.
    low_confidence_flag: bool = False


#: Difficulty bands, as upper bounds on the 0..1 `difficulty` scale.
#:
#: **One definition, shared by everything that bands.** The generator picks a
#: source question by band, and mastery will be keyed on `(clo_id,
#: difficulty_band)` in the next phase — on the platform side, which cannot
#: import the service. So it lives in contracts, which both sides already
#: import, rather than being written twice and drifting.
#:
#: `difficulty_band` is deliberately not stored on any model: it is derived from
#: `difficulty` here, and a stored copy is a second source of truth.
_BANDS: tuple[tuple[float, str], ...] = ((0.34, "easy"), (0.67, "medium"), (1.01, "hard"))


def band_of(difficulty: float | None) -> str | None:
    """Which band a difficulty falls in. `None` in, `None` out.

    An unknown difficulty is *unknown*, not "easy" — the same rule
    `CLOMastery.accuracy` follows for an unattempted outcome. Defaulting it would
    quietly file every unscored past question into the easiest band.
    """
    if difficulty is None:
        return None
    for upper, name in _BANDS:
        if difficulty < upper:
            return name
    return _BANDS[-1][1]


def band_range(band: str) -> tuple[float, float]:
    """The half-open `difficulty` range a band covers, for filtering."""
    lower = 0.0
    for upper, name in _BANDS:
        if name == band:
            return (lower, upper)
        lower = upper
    raise ValueError(f"unknown difficulty band {band!r}")


class CLO(BaseModel):
    clo_id: str
    text: str
    #: CLO extraction is *assisted, never asserted* — a human confirms the list
    #: before it becomes the spine (§7.3). In the MVP that happens at pack-load
    #: time, by the person running the loader (§9.2 #2).
    confirmed_by: str | None = None


class ExamPrepPack(BaseModel):
    offering_id: str
    tenant: str
    clos: list[CLO] = Field(default_factory=list)
    questions: list[QuestionRecord] = Field(default_factory=list)

    #: sha256 of the SOURCE DOCUMENT this pack was extracted from.
    #:
    #: Of the document bytes, not of the pack: hashing the pack would change
    #: whenever the parser improved, so re-running a better extractor over the
    #: same paper would look like a new document and silently double the bank.
    #: Hashing the bytes answers the question actually being asked — have we
    #: already imported this paper?
    #:
    #: `None` for a hand-written pack, which is the only kind that existed
    #: before extraction. An absent hash cannot be checked for duplication, and
    #: the loader says so rather than treating "unknown" as "new".
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class StudyPlanItem(BaseModel):
    clo_id: str
    #: Sized by a marks budget: a two-hour session is 100 marks, not 8 questions.
    marks_budget: int
    question_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None


class StudyPlan(BaseModel):
    offering_id: str
    items: list[StudyPlanItem] = Field(default_factory=list)


class RevisionPlanOutcome(BaseModel):
    """One learning outcome in the unbudgeted revision plan, with its questions.

    Distinct from `StudyPlanItem` rather than an extension of it, and the reason
    is the one `StudyPlanRequest` already gives for not widening a contract: a
    field that cannot apply is a field that lies. `StudyPlanItem` is sized by
    `marks_budget`, which this plan has no concept of — it lists every tagged
    question for an outcome, weakest outcome first, and does no arithmetic.
    Adding `clo_text` and full questions there would leave `marks_budget`
    optional-and-ignored on half its uses.
    """

    clo_id: str
    #: Carried, not looked up again by the browser. The plan is ordered by
    #: weakness and the heading has to name the outcome; a client that had to
    #: re-fetch CLOs to render a plan could render one that disagrees with it.
    clo_text: str
    #: This student's standing on this outcome, as counters rather than a score
    #: — the same reasoning `CLOMastery` gives. Zero attempts means "not
    #: practised yet", which is a different statement from "0 correct".
    attempts: int = 0
    correct: int = 0
    #: Whole records, so the browser never re-queries to render what it was
    #: already sent. Every provenance field §7.6 requires — `source_doc_id`,
    #: `page`, `low_confidence_flag` — is already on `QuestionRecord`, which is
    #: why nothing new was invented to carry them.
    questions: list[QuestionRecord] = Field(default_factory=list)


class RevisionPlan(BaseModel):
    """The deterministic revision plan, as a value.

    **Why this exists at all.** The same plan was already being produced, but
    encoded as markdown inside opaque text tokens — `## CLO-1 — …` and
    `_Your record: …_` — which the browser then had to parse back into
    structure. Presentation markup travelling through a data channel is what
    made `_Source: oex101_final_2024.pdf, p.2_` unparseable: the FILENAME
    contains underscores, so the italics could not be told from the data.

    Structure removes the collision rather than working around it. The rule this
    follows is the one `/study-plan` already states next door: a plan is a value,
    not a narration, and a value travels as JSON.

    The prose stream on `/plan` stays, because when `agent_enabled` is true the
    agent genuinely narrates — and prose does arrive a token at a time.
    """

    offering_id: str
    #: Weakest outcome first, capped. Order is meaning here, not presentation:
    #: it is the planner's recommendation about what to revise, and a client
    #: that re-sorted would be overriding the advice.
    outcomes: list[RevisionPlanOutcome] = Field(default_factory=list)


class PracticeQuestion(BaseModel):
    """Personal output (§9.0) — reaches one student, once.

    No instructor gate, deliberately: gating one student's private study aid is
    unworkable and protects nobody. What makes it safe instead is that it is
    labelled, it cites its source, and it is measured by the Feature B rubric
    (§11.3) — for this path, measurement *is* the control.
    """

    text: str
    clo_id: str | None = None
    #: Always true in the MVP. Present as a field because it is a claim we make
    #: to the student, not an implementation detail.
    ai_generated: bool = True
    #: The past paper or lesson this derives from (§7.6 provenance).
    derived_from: list[str] = Field(default_factory=list)

    # --- fields the Feature B rubric scores (added 2026-08-11) ---------------
    #
    # `eval/feature_b_rubric.py` reads `marks` and `difficulty` to check that a
    # generated question sits inside the ranges the real bank uses. This model
    # did not carry either, and the consequence was worse than a missing number:
    # the check reads `if marks is not None`, so an absent field made
    # `band_plausibility` **pass**. The rubric would have reported 1.00 on every
    # generated question — not because difficulty was calibrated, but because
    # there was no data to judge.
    #
    # Found by a contract-freeze pass before any generator existed, by comparing
    # what the rubric reads against what this model provides. Both halves were
    # written here and had never been run together.
    #
    # `difficulty_band` is deliberately NOT a field: it is derived from
    # `difficulty` by one shared helper, and storing it would be a second source
    # of truth that can disagree with the first.
    marks: int | None = Field(default=None, ge=0)
    #: 0..1, the same scale as `QuestionRecord.difficulty`, so the rubric can
    #: compare a generated question against the bank without converting.
    difficulty: float | None = Field(default=None, ge=0.0, le=1.0)
    #: §7.6 again: a derived difficulty stays labelled derived wherever it is
    #: shown. A generated question's difficulty is always an estimate.
    difficulty_is_derived: bool = True


# --- the request wire format ------------------------------------------------


class ExamPrepRequest(BaseModel):
    """Browser -> service, for one exam-prep turn.

    Deliberately shaped like `ChatRequest`: free-text intent plus platform-owned
    state carried by the browser. There is no `student_id` and no `offering_id`
    field, and their absence is the contract — scope comes from the verified JWT,
    so a forged payload cannot widen it. The tool registry refuses those fields
    from the model for the same reason one level down.
    """

    #: What the student asked for, verbatim. "Help me revise CLO-3", "give me a
    #: two-hour plan for the final". Free text on purpose: the point of an agent
    #: here is that this space is not enumerable.
    request: str = Field(min_length=1, max_length=1000)

    #: The memory layer, carried from platform-owned `Scope.user_state` (§3.1).
    #: `None` means the browser sent none, which is a legitimate state — a new
    #: student — and is reported to the agent as "no history", never as an error.
    mastery: MasterySnapshot | None = None


class PracticeRequest(BaseModel):
    """Browser -> service, for one generated practice question.

    Two fields, and the shape is the point: a student picks an outcome and a
    level, and everything else — who they are, which course, which past paper,
    which lesson — is derived server-side. There is no `student_id`,
    `offering_id` or `question_id` here, so a forged payload cannot widen scope
    or name a source; the JWT scopes the request and the generator selects the
    source itself.

    Deliberately NOT free text, unlike `ExamPrepRequest`. Generation is a fixed
    two-stage pipeline with no planning step, so there is nothing for prose to
    express — and an open field would invite prompt injection into a path that
    produces content a student reads.
    """

    clo_id: str = Field(min_length=1, max_length=64)
    #: `None` means "any level": the generator uses the source question's own
    #: band. Not defaulted to `easy` — an absent preference is not a preference,
    #: and guessing one would silently narrow what the student is offered.
    difficulty_band: Literal["easy", "medium", "hard"] | None = None


class StudyPlanRequest(BaseModel):
    """Browser -> service, for one budgeted study plan.

    Separate from `ExamPrepRequest` rather than a field added to it. That request
    is free text answered by prose (or by the agent); this one is a number
    answered by arithmetic, and the deterministic planner needs an integer it can
    rely on. Widening `ExamPrepRequest` would have made `marks_budget` optional on
    a route that cannot use it, and optional-and-ignored is how a field becomes a
    lie.

    Same absence as every other request contract here, and it is the contract:
    no `student_id`, no `offering_id`. Scope comes from the verified JWT, so a
    forged payload cannot widen it — `plan_for_offering` takes no `offering_id`
    parameter at all, which is the same rule enforced one level down.
    """

    #: §7.4's unit. A two-hour session is 100 marks, not 8 questions — "8
    #: questions" means nothing until you know whether they are 2-mark or 25-mark
    #: questions.
    #:
    #: Bounded at both ends. Zero or negative is not a session, and the planner
    #: already refuses it; rejecting it here means a caller learns it from a 422
    #: naming the field rather than from an empty plan they have to interpret.
    #: The ceiling is what stops an absurd budget turning into a query for every
    #: question in the bank — a real exam is well under 500 marks.
    marks_budget: int = Field(ge=1, le=500)

    #: The memory layer, carried from platform-owned `Scope.user_state` (§3.1),
    #: exactly as `ExamPrepRequest` carries it. `None` means the browser sent
    #: none, which is a legitimate state — a new student — and shapes the plan as
    #: "nothing practised yet", never as an error. A snapshot minted for another
    #: offering is ignored by the planner, not trusted.
    mastery: MasterySnapshot | None = None


class CLOOption(BaseModel):
    """One entry in the exam-prep tab's outcome selector.

    Mirrors what the agent's `get_plan_context` tool already returns, so the UI
    and the model see the same shape. `confirmed_by` is deliberately reduced to a
    boolean: §7.3 wants the student to know whether a human confirmed the
    outcome, not which human — that is an instructor's name and no part of a
    study aid.
    """

    clo_id: str
    text: str
    confirmed: bool = False


class ExamPrepStatus(BaseModel):
    """What the XBlock renders before the student asks anything.

    Exists so the tab can say *why* it is empty. Design §5.1's whole argument is
    that "still being prepared" and "not covered" are different sentences, and a
    tab that renders a disabled box with neither is the failure it describes.
    """

    #: False when `agent_enabled` is off. The tab then offers the deterministic
    #: path rather than a button that does nothing.
    agent_available: bool
    #: Whether past papers were loaded for this offering.
    pack_loaded: bool
    questions: int = 0
    clos: int = 0
    #: Questions the extractor was unsure about (§7.6). Surfaced rather than
    #: hidden: a student should know the bank has soft spots.
    low_confidence: int = 0
    earliest_year: int | None = None
    latest_year: int | None = None
    #: The outcomes the practice selector offers.
    #:
    #: Named `clo_options`, not `clos`, because `clos` above is already the COUNT
    #: of outcomes and both are useful — the count for the "3 outcomes" summary
    #: line, the list for the dropdown. Reusing the name would have silently
    #: replaced an int with a list on a field the browser already reads.
    #:
    #: Carried on `/status` rather than behind a second endpoint: the tab already
    #: calls this before enabling anything, and an extra round trip to fill a
    #: dropdown is a round trip the student waits through.
    clo_options: list[CLOOption] = Field(default_factory=list)
