"""Unenrollment invalidation carries an identifier the service can match.

**The bug these exist to prevent shipped twice.** First as `user.id`, which
built `cm:authz:12345:{offering}` while the service had written
`cm:authz:alice:{offering}`. Then as the *fix* for it — `getattr(user,
"username", "")` — which is worse, because it reads as correct: openedx-events
does not put username on the user object. `UserData` carries `id`, `is_active`
and `pii`, and the username is on the nested `UserPersonalData`. So the getattr
returned the default, fell through to the numeric id, and restored the original
bug behind a line that looks like its repair.

Neither version raises, neither is logged, and the notice returns 200 with
`dropped=0` — which is also what a genuinely empty cache returns. The only way
to tell them apart is to assert on the identifier, which is what this file does.

The payload is reconstructed here rather than imported: `openedx_events` is a
platform dependency, and a test that needs Open edX installed is a test that
does not run. The shape is pinned against
`openedx_events/learning/data.py` — if upstream moves username, these fail,
which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

from coursemate_platform.events.lms_receivers import student_identifier


@dataclass(frozen=True)
class _UserPersonalData:
    """Mirrors openedx_events.learning.data.UserPersonalData."""

    username: str
    email: str = "s@example.com"
    name: str = "A Student"


@dataclass(frozen=True)
class _UserData:
    """Mirrors openedx_events.learning.data.UserData.

    Note what is absent: there is no `username` field. That absence is the
    entire subject of this test module, so it is modelled faithfully rather
    than conveniently.
    """

    id: int
    is_active: bool = True
    pii: _UserPersonalData | None = None


def test_username_comes_from_pii():
    user = _UserData(id=12345, pii=_UserPersonalData(username="alice"))
    assert student_identifier(user) == "alice"


def test_the_numeric_id_is_never_used_as_the_identifier():
    """The original bug, stated directly.

    The service keys its authz cache on the username, so a numeric id is not a
    weaker identifier — it is one that matches no key that will ever exist.
    """
    user = _UserData(id=12345, pii=_UserPersonalData(username="alice"))
    assert student_identifier(user) != "12345"


def test_a_bare_attribute_lookup_would_have_missed_it():
    """Pins the reason the previous fix was a no-op.

    If this ever starts returning a username, upstream has flattened the
    payload and `student_identifier` can be simplified. Until then, anything
    reaching for `user.username` is reaching for something that is not there.
    """
    user = _UserData(id=12345, pii=_UserPersonalData(username="alice"))
    assert getattr(user, "username", "") == ""


def test_missing_pii_widens_to_the_whole_offering():
    """Fail safe, not fail quiet.

    An empty student makes `verifier.invalidate` clear every entitlement for the
    offering. That costs those students one platform round-trip each and
    guarantees the revocation lands — which is the correct direction to err,
    unlike returning an id that silently matches nothing.
    """
    assert student_identifier(_UserData(id=12345, pii=None)) == ""


def test_blank_username_widens_too():
    assert student_identifier(_UserData(id=1, pii=_UserPersonalData(username=""))) == ""
