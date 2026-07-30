"""The student-facing chat contract — design §3.1, §8.5, §13.1.

Chat history is owned by the platform in `Scope.user_state` and *carried* by the
browser (§3.1, v8). The service holds no conversation state: it receives a rolling
window, answers, and forgets. The completed turn is written back through the
XBlock, which is what keeps "the platform keeps chat private" literally true and
keeps every byte of it inside user-retirement's reach.

History is never written to our logs or the response cache (§6.4).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .errors import ErrorCode


class Role(StrEnum):
    STUDENT = "student"
    TUTOR = "tutor"


class Turn(BaseModel):
    role: Role
    content: str


class Mode(StrEnum):
    DIRECT = "direct"
    #: Returns a guiding question first for conceptual asks. Does **not** relax
    #: grounding — the guiding question is itself derived from retrieved content
    #: (§8.5).
    SOCRATIC = "socratic"


class ChatRequest(BaseModel):
    question: str
    #: Rolling window, trimmed by token budget platform-side. Bounded on purpose:
    #: the payload cost is a few KB, and the alternative is a second PII store.
    history: list[Turn] = Field(default_factory=list, max_length=20)
    mode: Mode = Mode.DIRECT
    #: Where the student is standing. Used as a retrieval hint, never as authz —
    #: scope comes from the token plus a live enrollment check.
    usage_key: str | None = None


class Citation(BaseModel):
    """Mandatory. An answer that cannot cite abstains (§8.5)."""

    usage_key: str
    display_name: str | None = None
    #: Deep link into the courseware, so citation validity is checkable by a
    #: human rater rather than asserted (§11.2b).
    url: str | None = None


class FrameType(StrEnum):
    TOKEN = "token"
    CITATION = "citation"
    #: Raised within ~500ms of completion when parallel verification finds a
    #: claim no retrieved chunk supports (§8.5). Marks the sentence; never
    #: silently rewrites text the student already read.
    UNSUPPORTED_CLAIM = "unsupported_claim"
    #: The answer came from a fallback tier. Surfaced so an outage does not read
    #: as "the tutor got worse this week" (§8.4 rule 3).
    DEGRADED = "degraded"
    ERROR = "error"
    DONE = "done"


class StreamFrame(BaseModel):
    """One SSE frame, browser-bound."""

    type: FrameType
    text: str | None = None
    citation: Citation | None = None
    error_code: ErrorCode | None = None
    #: Which provider answered, for the trace (§11.4).
    provider: str | None = None


class PersistTurnRequest(BaseModel):
    """Browser -> XBlock `json_handler`, after the stream completes.

    The service never writes this; the platform does. A dropped connection can
    therefore lose the last turn — a visible, recoverable annoyance, and cheaper
    than duplicating PII into a store that retirement does not reach (§3.1).
    """

    question: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
