"""Exam prep — design §7.

The structured question record (§7.6) is the point of the feature. Extracting only
question *text* throws away the structure the whole thing runs on: "practice for
CLO-3, from final papers, 2023 onward, worth 10+ marks" is a metadata filter over
records, not a semantic search over blobs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


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

    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_method: ExtractionMethod | None = None
    #: Low-confidence items are shown as such rather than quietly trusted.
    low_confidence_flag: bool = False


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


class StudyPlanItem(BaseModel):
    clo_id: str
    #: Sized by a marks budget: a two-hour session is 100 marks, not 8 questions.
    marks_budget: int
    question_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None


class StudyPlan(BaseModel):
    offering_id: str
    items: list[StudyPlanItem] = Field(default_factory=list)


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
