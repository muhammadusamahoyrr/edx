"""Roles reach the token as whole words.

`XBlockUser.opt_attrs` reports the course role as a **string**. The mint handler
called `list()` on it, so `"staff"` was minted as `['s', 't', 'a', 'f', 'f']` —
on every token, for the life of the project. Nothing failed: `StudentClaims`
accepts any list of strings, and the only consumer that reads roles is dormant.

The line immediately after it looked like the fix:

    roles=roles if isinstance(roles, list) else [str(roles)]

which is why the bug lasted. It is a guard for the string case, placed after the
string had already been destroyed, so it could never fire.
"""

from __future__ import annotations

from coursemate_platform.xblock.identity import ROLE_ATTR, roles_of


def test_a_role_string_becomes_one_role():
    """The bug itself. `list("staff")` is five roles; this must be one."""
    assert roles_of({ROLE_ATTR: "staff"}) == ["staff"]


def test_the_role_is_not_spelled_out_letter_by_letter():
    """Stated the way the defect actually looked, so a regression is obvious."""
    assert roles_of({ROLE_ATTR: "student"}) != ["s", "t", "u", "d", "e", "n", "t"]
    assert len(roles_of({ROLE_ATTR: "student"})) == 1


def test_instructor_too():
    assert roles_of({ROLE_ATTR: "instructor"}) == ["instructor"]


def test_a_list_is_passed_through():
    """Some runtimes may hand back a sequence. Accept both without guessing."""
    assert roles_of({ROLE_ATTR: ["staff", "instructor"]}) == ["staff", "instructor"]


def test_no_role_is_no_roles():
    assert roles_of({ROLE_ATTR: ""}) == []
    assert roles_of({ROLE_ATTR: None}) == []
    assert roles_of({}) == []
    assert roles_of(None) == []


def test_whitespace_is_not_a_role():
    assert roles_of({ROLE_ATTR: "   "}) == []
    assert roles_of({ROLE_ATTR: " staff "}) == ["staff"]


def test_an_unreadable_value_yields_no_roles():
    """Roles are informational and staff access is re-checked against the
    platform, so inventing one from an object we do not understand buys nothing
    and could only ever be wrong."""
    assert roles_of({ROLE_ATTR: 42}) == []
