"""Token minting — design §3.4 rule 3 (v8).

These run in a plain venv with no Open edX, which is the point of the fast/slow
test split: a suite that needs Tutor is a suite nobody runs.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from coursemate_contracts.auth import AUDIENCE_SERVICE, AUDIENCE_STUDENT
from coursemate_platform.client.jwt import (
    WeakSigningKey,
    decode_for_test,
    mint_student_token,
)

KEY = "test-signing-key-at-least-32-bytes-long"


def _mint(**overrides):
    args = dict(
        signing_key=KEY,
        user_id="u1",
        course_id="course-v1:ACME+CS101+2026",
        offering_id="CS101-2026-FALL",
        roles=["student"],
        usage_key="block-v1:ACME+CS101+2026+type@vertical+block@abc",
    )
    args.update(overrides)
    return mint_student_token(**args)


def test_mint_carries_the_claims_the_service_needs():
    claims = decode_for_test(_mint().token, KEY)
    assert claims["sub"] == "u1"
    assert claims["offering_id"] == "CS101-2026-FALL"
    assert claims["aud"] == AUDIENCE_STUDENT


def test_token_is_short_lived():
    """Minutes, not hours. It has to outlive a conversation turn and no more."""
    claims = decode_for_test(_mint().token, KEY)
    lifetime = claims["exp"] - claims["iat"]
    assert 0 < lifetime <= 900


def test_student_token_is_not_a_service_token():
    """A leaked student-path token must not be able to write to the index (§3.4).
    Different audience is the mechanism that makes that a verification failure
    rather than a policy."""
    claims = decode_for_test(_mint().token, KEY)
    assert claims["aud"] != AUDIENCE_SERVICE


def test_expired_token_is_rejected():
    token = _mint(ttl_seconds=1).token
    time.sleep(1.2)
    with pytest.raises(pyjwt.ExpiredSignatureError):
        decode_for_test(token, KEY)


def test_wrong_key_is_rejected():
    with pytest.raises(pyjwt.InvalidSignatureError):
        decode_for_test(_mint().token, "another-key-that-is-also-32-bytes-long")


def test_a_short_signing_key_fails_at_mint_time():
    """RFC 7518 §3.2. This hop is the one Open edX does not secure for us, so a
    weak secret fails loudly rather than warning into a log nobody reads."""
    with pytest.raises(WeakSigningKey):
        _mint(signing_key="too-short")


def test_token_is_bound_to_a_usage_key():
    """So it cannot be replayed against a different unit's tutor."""
    claims = decode_for_test(_mint().token, KEY)
    assert claims["usage_key"].endswith("block@abc")


def test_response_points_at_a_same_origin_path_not_a_hostname():
    """§3.4 (v8): the service is exposed as a path under the LMS origin, never as
    a second published host. A response carrying an absolute URL to another host
    would quietly undo that."""
    response = _mint()
    assert response.stream_path.startswith("/")
    assert "://" not in response.stream_path
