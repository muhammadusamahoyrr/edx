"""LMS-side receivers — design §3.4 rule 4.

These exist because **the events we depend on fire in two different processes.**
`content_authoring` events fire in Studio; `learning` events fire in the LMS. An
earlier draft of the design put every receiver in the CMS, which would have left
enrollment changes entirely unobserved — and two guarantees depend on seeing them:
the metadata cache invalidates on enrollment (§6.4), and §10.7 scopes personal
data on unenrollment.

The metadata cache is deliberately the shortest-lived tier precisely because a
*revoked* enrollment must stop working quickly. This receiver is what makes that
immediate rather than a TTL wait.
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger(__name__)


def student_identifier(user) -> str:
    """The value the service caches entitlements under — `user.pii.username`.

    **Username is not an attribute of the event's user object.** openedx-events
    passes `UserData`, whose fields are `id`, `is_active` and `pii`; the username
    lives on the nested `UserPersonalData` as `pii.username`. `user.username`
    does not exist and a `getattr` for it returns the default, silently.

    That distinction is the whole correctness of this function. The service
    writes its authz cache under the username — `boundary/impl.py` passes
    `claims.username or claims.sub` to `require_enrolled`, which keys
    `cm:authz:{username}:{offering}`. Sending the numeric id instead builds
    `cm:authz:12345:{offering}`, which matches nothing: the notice returns 200,
    `dropped=0` is not an error, nothing is logged, and the unenrolled student
    keeps their access until the 60s TTL runs out. An invalidation that cannot
    match is indistinguishable from one that had nothing to clear.

    **Returning "" is deliberate, and it fails safe.** An empty student widens
    the notice to the whole offering (`verifier.invalidate` treats a missing
    user as "every user in this course"), which costs those students one
    platform round-trip each and guarantees the revocation lands. Blunt, and the
    right direction to be blunt in: the alternative is an identifier known not
    to match, which is a no-op reporting success.
    """
    pii = getattr(user, "pii", None)
    username = str(getattr(pii, "username", "") or "")
    if username:
        return username

    log.error(
        "coursemate: unenrollment event carried no pii.username (user id=%r); "
        "falling back to invalidating the whole offering",
        getattr(user, "id", None),
    )
    return ""


def on_unenrollment(signal, sender, enrollment, **kwargs):
    """COURSE_UNENROLLMENT_COMPLETED.

    Thin, like every receiver: post an invalidation notice and return. Two things
    happen service-side as a result — the permission cache entry is dropped, and
    that student's course-linked personal data is scoped or purged (§10.7).
    """
    from ..tasks.invalidate import post_invalidation

    user = getattr(enrollment, "user", None)
    course = getattr(enrollment, "course", None)

    post_invalidation.delay(
        reason="unenrolled",
        course_id=str(getattr(course, "course_key", "") or ""),
        student_id=student_identifier(user),
        trace_id=str(uuid.uuid4()),
    )
    log.info("coursemate: enqueued unenrollment invalidation")
