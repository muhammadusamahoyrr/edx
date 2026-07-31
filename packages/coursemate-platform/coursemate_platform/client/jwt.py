"""Minting the student token — design §3.4, as changed in v8.

This is the whole of the XBlock's involvement in an answer. It mints, returns, and
the LMS worker is free; the browser streams from the service directly. Under the
old proxy shape this module would have been a relay holding a gunicorn worker for
the full five-to-fifteen seconds of a generation — which is the same worker
exhaustion §3.4 was written to prevent, since the pool is exhausted by occupancy
rather than computation.

The signing key comes from Django settings, never from an XBlock field: there is
no mechanism that excludes a Scope.settings field from OLX export (design v7,
§10.4), so the only safe place for a secret is somewhere the course package
cannot reach.
"""

from __future__ import annotations

import time

import jwt
from coursemate_contracts.auth import (
    AUDIENCE_STUDENT,
    DEFAULT_TTL_SECONDS,
    StudentClaims,
    TokenResponse,
)

ALGORITHM = "HS256"

#: RFC 7518 §3.2: an HMAC key must be at least as long as the hash output. A
#: shorter shared secret weakens the one signature the platform does not provide
#: for us, so it fails at mint time rather than warning into a log nobody reads.
MIN_SIGNING_KEY_BYTES = 32


class WeakSigningKey(ValueError):
    """The service hop's signing key is too short to be worth having."""


def _require_strong_key(signing_key: str) -> None:
    if len(signing_key.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
        raise WeakSigningKey(
            f"CourseMate signing key is {len(signing_key.encode('utf-8'))} bytes; "
            f"{MIN_SIGNING_KEY_BYTES} are required for {ALGORITHM} (RFC 7518 §3.2)."
        )


def mint_student_token(
    *,
    signing_key: str,
    user_id: str,
    username: str | None = None,
    course_id: str = "",
    offering_id: str,
    roles: list[str],
    usage_key: str | None = None,
    block_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    stream_path: str = "/coursemate/api/chat",
) -> TokenResponse:
    """Mint a short-lived token for one conversation turn.

    The claims are a statement of *who is asking*, not a grant of access: the
    service re-derives enrollment and role at the boundary on every call (§6.5,
    §10.1). A forged or replayed claim of enrollment therefore buys nothing —
    which mattered before v8 and matters more now that a browser holds the token.
    """
    _require_strong_key(signing_key)
    now = int(time.time())
    claims = StudentClaims(
        sub=user_id,
        username=username,
        course_id=course_id,
        offering_id=offering_id,
        roles=roles,
        aud=AUDIENCE_STUDENT,
        exp=now + ttl_seconds,
        iat=now,
        usage_key=usage_key,
        block_id=block_id,
    )
    token = jwt.encode(claims.model_dump(), signing_key, algorithm=ALGORITHM)
    return TokenResponse(
        token=token, expires_in=ttl_seconds, stream_path=stream_path
    )


def decode_for_test(token: str, signing_key: str) -> dict:
    """Verification lives in the service; this exists so the platform's own tests
    can assert on what was minted without importing the service package."""
    return jwt.decode(
        token, signing_key, algorithms=[ALGORITHM], audience=AUDIENCE_STUDENT
    )
