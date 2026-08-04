"""The student hop's JWT — design §3.4, as changed in v8.

The XBlock mints; the **browser** carries it and streams from the service directly.
No LMS worker is held for the duration of an answer (§3.4 rule 3).

Two properties this token deliberately does not have:

* It is **not a grant of access.** It establishes *who is asking*. The service
  re-derives enrollment and role at the boundary on every call (§6.5, §10.1), so
  a forged or replayed claim of enrollment buys nothing. This mattered before v8
  and matters more now that a browser handles the token.
* It is **not long-lived.** Minutes, not the seconds of the old server-to-server
  hop — it has to outlive a conversation turn — and refreshed by another handler
  call when it expires.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Minted with this expiry. Long enough for one turn, short enough that a leaked
#: token is worth little.
DEFAULT_TTL_SECONDS = 300

#: Separate credential class for ingest/invalidation (§3.4) — a leaked
#: student-path token must not be able to write to the index.
AUDIENCE_STUDENT = "coursemate:student"
AUDIENCE_SERVICE = "coursemate:service"


class StudentClaims(BaseModel):
    """Claims the XBlock mints into the student token.

    Deliberately small. Everything here answers *who is asking and from where*;
    nothing here grants access. The service re-derives authorization on every
    request, so a forged claim buys nothing (§6.5, §10.1).
    """

    sub: str  # user_id (numeric, stable)
    #: The platform's enrollment API keys on USERNAME, not numeric id, so it is
    #: carried explicitly rather than looked up on every authorization check.
    username: str | None = None
    course_id: str
    offering_id: str
    #: Platform roles as the LMS reports them. Informational only: anything
    #: staff-only is re-checked against the platform before it is honoured.
    roles: list[str] = Field(default_factory=list)
    aud: str = AUDIENCE_STUDENT
    iss: str = "coursemate-xblock"
    exp: int
    iat: int
    #: Ties a token to one rendered block, so it cannot be replayed against a
    #: different unit's tutor. `usage_key` is the fully-qualified locator;
    #: `block_id` is its short form, carried separately because retrieval filters
    #: and audit records key on it directly (§6.2).
    usage_key: str | None = None
    block_id: str | None = None
    #: Access groups the caller belongs to, as `"<partition>:<group>"` tokens,
    #: resolved server-side at mint time through the platform's PartitionService.
    #:
    #: **This is the one claim the service acts on rather than re-deriving, and
    #: the reason is worth stating.** Re-deriving it would mean hard-coding
    #: partition and group ids that are per-instance configuration — right on one
    #: deployment and quietly wrong on the next, in the direction that shows a
    #: paying student less than they paid for. Minting them inside the LMS uses
    #: the platform's own partition logic instead.
    #:
    #: The cost is bounded staleness: a track or cohort change takes effect at
    #: the next mint rather than instantly, so at most `DEFAULT_TTL_SECONDS`.
    #: That is the same order as the 60 s authorization cache, and unlike
    #: enrollment there is no revocation event to make it immediate. Enrollment
    #: itself is still re-derived on every call and still fails closed.
    group_tokens: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    """What `json_handler` returns to the browser. Note what is absent: no
    answer, no retrieval, no model output — this handler returns in milliseconds."""

    token: str
    expires_in: int = DEFAULT_TTL_SECONDS
    #: Same-origin path the browser streams from (§3.4). Never a second hostname.
    stream_path: str = "/coursemate/api/chat"
