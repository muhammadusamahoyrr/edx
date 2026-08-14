"""Authoring permission, asked of the platform — the second adapter.

`content_adapter` exists because §3.3 wants every *content* read behind one
module. This one exists for the same reason one layer over: it is the only place
CourseMate asks Open edX **"may this person author this course?"**, and it is
here rather than in the XBlock because `.importlinter` contract 1 forbids
`coursemate_platform.xblock` from importing `common`, `lms`, `cms`, `openedx` or
`xmodule` at all.

That contract is worth obeying rather than exempting. Its list is broader than
its name — it is not only about the modulestore, it is the rule that **platform
coupling lives in `adapters/` and nowhere else**. The XBlock now depends on a
CourseMate function with a two-argument signature; when Open edX moves
`has_studio_write_access`, this file changes and nothing else does.

**Every platform import is inside the function, unlike `content_adapter`, whose
imports are at module scope.** The difference is who imports the module.
`content_adapter` is reached from Celery tasks and management commands, which
already run inside a full platform. This one is reached from `xblock/tutor_block`,
which is imported during **LMS startup for every course on the instance**
(Principle 8). Keeping the imports lazy means importing this module costs nothing
and can never be the thing that slows or breaks a boot — and it keeps the module
importable in the offline unit suite, which has Django and XBlock but no
edx-platform.

**Any failure to resolve the caller is "not proven", which means refused.**
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def can_author(user_id, course_id: str) -> bool:
    """Whether `user_id` may change authoring settings on `course_id`.

    **The platform's own authoring permission, not a proxy for it.** Three
    cheaper signals were tried against the live stack and all three are wrong on
    the path this runs on:

      `user_role`              Studio never sets it. `DjangoXBlockUserService`
                               defaults it to "student", so every author looked
                               like a learner and was refused.
      `user_is_staff`          The same. CMS constructs that service with no
                               kwargs, so it is always False in Studio.
      `runtime.is_author_mode` Set when Studio loads a block for PREVIEW, but
                               `component_handler` fetches the block straight
                               from the modulestore, so the handler route never
                               has it.

    `has_studio_write_access` is what Studio itself gates authoring on, so it is
    right in Studio — and it refuses a learner on the LMS route, where the
    handler is equally reachable and nothing else would stop them.
    """
    from django.contrib.auth import get_user_model
    from opaque_keys.edx.keys import CourseKey

    from common.djangoapps.student.auth import has_studio_write_access

    try:
        django_user = get_user_model().objects.get(id=user_id)
        course_key = CourseKey.from_string(course_id)
    except Exception:  # noqa: BLE001 - an unresolvable caller is not an author
        log.warning("studio edit refused: caller could not be resolved")
        return False

    return bool(has_studio_write_access(django_user, course_key))
