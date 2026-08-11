"""Reading identity off the XBlock user service — pure, so it is testable.

Extracted from `tutor_block.py` for the reason `citations.py` gives: that module
imports `django.conf` at import time, and a function behind a framework import
is a function the fast suite never exercises.

**The bug this was extracted to fix.** `XBlockUser.opt_attrs` reports the role
as a **string** — `"student"`, `"staff"`, `"instructor"` — not a list. The mint
handler did:

    roles = list(opt_attrs.get("edx-platform.user_role", "") or "")

which iterates the string and produces `['s', 't', 'a', 'f', 'f']`. Every token
minted for a staff member carried five single-letter roles.

It survived because the next line looked like it handled exactly this:

    roles=roles if isinstance(roles, list) else [str(roles)]

That guard can never fire — `list()` on the line above has already made it a
list — so the code reads as if the string case is covered while being the thing
that broke it. Same shape as the `UNSUPPORTED_CLAIM` frame with no producer: a
branch present, handled, and unreachable.

Harmless today only by luck. `StudentClaims.roles` is documented informational,
and the one consumer that folds roles into a key — `knowledge/cache/keys.py` —
is dormant. The moment anything asks `"staff" in claims.roles`, it is wrong, and
wrong in the direction that denies a real instructor.
"""

from __future__ import annotations

#: What the LMS puts in `opt_attrs` for the current user's course role.
ROLE_ATTR = "edx-platform.user_role"


def roles_of(opt_attrs) -> list[str]:
    """The caller's platform roles, always as a list of whole role names.

    Accepts either shape without guessing: a bare string becomes a one-element
    list, an existing sequence is normalised element-wise. Anything falsy is no
    roles at all, which is the safe reading — roles are informational here and
    the service re-checks staff access against the platform regardless (§10.1),
    so an empty list costs nothing and a fabricated one would not be honoured.
    """
    raw = (opt_attrs or {}).get(ROLE_ATTR) or ""

    if isinstance(raw, str):
        role = raw.strip()
        return [role] if role else []

    try:
        return [str(r).strip() for r in raw if str(r).strip()]
    except TypeError:
        # Not a string and not iterable. Record nothing rather than invent a
        # role from an object we do not understand.
        return []
