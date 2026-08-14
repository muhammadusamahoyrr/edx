"""Saving the Studio settings panel.

Written because `exam_prep_enabled` was unreachable (2026-08-13). The field
exists, defaults to False, and gates the whole exam-prep tab in
`student_view.html` — but the Studio panel offered no control for it, and a
custom `studio_view` REPLACES Studio's automatic settings editor, so leaving it
out did not fall back to anything. There was also no `submit_studio_edits`
handler at all: the other three controls wrote nowhere either.

Two properties are pinned here, and the second matters more than it looks:

  1. the handler writes every settings field, including the one that was missing
  2. it refuses a caller who is not course staff

An XBlock handler is reachable through the LMS as well as Studio, at
`/courses/<id>/xblock/<usage>/handler/submit_studio_edits`, and the LMS does not
apply Studio's write-access check. Without the role guard, any enrolled student
could disable the tutor for a whole course through a URL. That is a bigger
defect than the one this change set out to fix, so it gets the louder test.

Methods are exercised unbound against a stand-in, following
`test_exam_prep_block.py`: instantiating a real XBlock needs a runtime, and the
runtime contributes nothing to the logic under test.
"""

from __future__ import annotations

import types

from coursemate_platform.xblock.tutor_block import CourseMateTutorXBlock


class _Stub:
    """Enough of the block to run the handler.

    `_can_author` is stubbed rather than exercised: the real one calls
    `has_studio_write_access`, which lives in edx-platform, and this suite runs
    with Django and SQLite alone by design. What the real check does is pinned
    separately, at the bottom of this file.
    """

    def __init__(self, can_author=True, user=True):
        self._can = can_author
        self._has_user = user
        self.display_name = "CourseMate Tutor"
        self.enabled = True
        self.exam_prep_enabled = False
        self.mode = "direct"

    def _user(self):
        if not self._has_user:
            return None
        return types.SimpleNamespace(
            opt_attrs={"edx-platform.user_id": "7", "edx-platform.username": "teacher"}
        )

    def _can_author(self, user):
        return self._can


def _save(stub, payload):
    return CourseMateTutorXBlock.submit_studio_edits.__wrapped__(stub, payload)


# --- the field that was missing -------------------------------------------


def test_exam_prep_can_be_turned_on():
    """The whole point. Before this handler existed there was no way to reach
    this field from Studio at all, so every block created through the UI had
    the exam-prep tab permanently off."""
    stub = _Stub()
    assert stub.exam_prep_enabled is False

    result = _save(stub, {"exam_prep_enabled": True})

    assert result["saved"] is True
    assert stub.exam_prep_enabled is True
    assert result["exam_prep_enabled"] is True


def test_exam_prep_can_be_turned_off_again():
    stub = _Stub()
    stub.exam_prep_enabled = True
    _save(stub, {"exam_prep_enabled": False})
    assert stub.exam_prep_enabled is False


def test_every_settings_field_is_writable():
    stub = _Stub()
    _save(stub, {
        "display_name": "Revision helper",
        "enabled": False,
        "exam_prep_enabled": True,
        "mode": "socratic",
    })
    assert (stub.display_name, stub.enabled, stub.exam_prep_enabled, stub.mode) == (
        "Revision helper", False, True, "socratic"
    )


def test_omitted_fields_are_left_alone():
    """A partial payload must not reset the fields it does not mention."""
    stub = _Stub()
    stub.mode = "socratic"
    stub.display_name = "Kept"
    _save(stub, {"exam_prep_enabled": True})
    assert stub.mode == "socratic"
    assert stub.display_name == "Kept"


# --- authorization ---------------------------------------------------------


def test_a_caller_without_authoring_rights_changes_nothing():
    """The one that matters. This handler is reachable from the LMS as well as
    Studio, and the LMS applies no authoring check of its own — without this
    guard an enrolled learner could disable the tutor for a whole course."""
    stub = _Stub(can_author=False)
    result = _save(stub, {"enabled": False, "exam_prep_enabled": True})

    assert result == {"error": "forbidden"}
    assert stub.enabled is True
    assert stub.exam_prep_enabled is False


def test_the_guard_runs_before_any_field_is_read():
    """A refusal that had already written half the payload would be worse than
    no guard, because the block and the panel would disagree."""
    stub = _Stub(can_author=False)
    _save(stub, {"display_name": "Hijacked", "mode": "socratic"})
    assert stub.display_name == "CourseMate Tutor"
    assert stub.mode == "direct"


def test_an_author_is_allowed():
    assert _save(_Stub(can_author=True), {"exam_prep_enabled": True})["saved"] is True


def test_anonymous_is_refused_before_anything_is_read():
    stub = _Stub(user=False)
    assert _save(stub, {"enabled": False}) == {"error": "unauthenticated"}
    assert stub.enabled is True


# --- validation ------------------------------------------------------------


def test_an_unknown_mode_is_rejected_rather_than_stored():
    """`mode` feeds a prompt path that understands two values. A third would
    fail later and elsewhere, as a service error nobody could trace back to a
    Studio dropdown."""
    stub = _Stub()
    assert _save(stub, {"mode": "socratic-plus"}) == {"error": "invalid_mode"}
    assert stub.mode == "direct"


def test_an_invalid_mode_blocks_the_whole_save():
    """Rejected as one unit: a half-applied save leaves the panel disagreeing
    with the block, and the author cannot tell which half landed."""
    stub = _Stub()
    _save(stub, {"exam_prep_enabled": True, "mode": "nope"})
    assert stub.exam_prep_enabled is False


def test_an_empty_display_name_is_ignored():
    stub = _Stub()
    _save(stub, {"display_name": "   "})
    assert stub.display_name == "CourseMate Tutor"


def test_checkbox_values_are_coerced_to_bool():
    stub = _Stub()
    _save(stub, {"exam_prep_enabled": "on", "enabled": 0})
    assert stub.exam_prep_enabled is True
    assert stub.enabled is False


def test_a_non_dict_payload_is_refused():
    stub = _Stub()
    assert _save(stub, ["exam_prep_enabled"]) == {"error": "bad_request"}


def test_unknown_keys_are_ignored():
    stub = _Stub()
    result = _save(stub, {"exam_prep_enabled": True, "secret_admin_flag": True})
    assert result["saved"] is True
    assert not hasattr(stub, "secret_admin_flag")


# --- the panel itself ------------------------------------------------------
#
# The handler alone does not fix this. The bug had two halves: no way to save,
# and no control to save FROM. Both are pinned, because either one missing
# leaves the field unreachable exactly as before.

import re
from pathlib import Path

XBLOCK = Path(__file__).resolve().parents[2] / "coursemate_platform" / "xblock"
TEMPLATE = XBLOCK / "static" / "html" / "studio_view.html"
BLOCK_SRC = XBLOCK / "tutor_block.py"
STUDIO_JS = XBLOCK / "static" / "js" / "src" / "studio.js"


def _render(context):
    from django.template import Context, Engine

    return Engine().from_string(TEMPLATE.read_text(encoding="utf-8")).render(
        Context(context)
    )


def _ctx(**over):
    base = {"display_name": "T", "enabled": True, "exam_prep_enabled": False,
            "mode": "direct", "last_indexed": "never", "block_count": 0}
    base.update(over)
    return base


def test_the_panel_offers_an_exam_prep_control():
    html = _render(_ctx())
    assert re.search(r'class="[^"]*\bcm-cfg-exam-prep\b', html), \
        "no exam-prep control — the field stays unreachable from Studio"


def test_the_control_reflects_the_stored_value():
    on = _render(_ctx(exam_prep_enabled=True))
    off = _render(_ctx(exam_prep_enabled=False))
    box_on = re.search(r'class="cm-cfg-exam-prep"[^>]*', on).group(0)
    box_off = re.search(r'class="cm-cfg-exam-prep"[^>]*', off).group(0)
    assert "checked" in box_on
    assert "checked" not in box_off


def test_the_panel_offers_a_save_button():
    assert re.search(r'class="[^"]*\bcm-cfg-save\b', _render(_ctx()))


def test_studio_view_passes_the_flag_into_the_template():
    """The control cannot render without the context key, and the key was
    missing when the control was first added — the two halves of a template
    binding fail independently."""
    src = BLOCK_SRC.read_text(encoding="utf-8")
    body = src.split("def studio_view", 1)[1].split("def ", 1)[0]
    assert '"exam_prep_enabled": self.exam_prep_enabled' in body


def test_studio_js_posts_every_settings_field():
    js = STUDIO_JS.read_text(encoding="utf-8")
    assert "submit_studio_edits" in js, "studio.js never calls the save handler"
    for field in ("display_name", "enabled", "exam_prep_enabled", "mode"):
        assert field in js, f"{field} is not sent by the panel"


# --- what the real guard uses ----------------------------------------------
#
# The decision cannot run here — it calls into edx-platform — so what is pinned
# is which primitive it calls. Three cheaper signals were tried against the live
# stack first and all three were wrong: `user_role` and `user_is_staff` are
# never populated by Studio's user service, and `runtime.is_author_mode` is set
# only on the preview route, not the handler route. Swapping back to any of them
# would look correct in a unit test and refuse every real author.
#
# **It moved to `adapters/studio_authz.py` on 2026-08-14.** `.importlinter`
# contract 1 forbids `coursemate_platform.xblock` from importing `common`, and
# doing the lookup in the block broke the build. These tests follow the code
# rather than being deleted with it: what they defend is the primitive, not the
# file it lives in.

AUTHZ_SRC = (
    Path(__file__).resolve().parents[2]
    / "coursemate_platform" / "adapters" / "studio_authz.py"
)


def test_the_guard_uses_the_platforms_own_authoring_permission():
    body = AUTHZ_SRC.read_text(encoding="utf-8").split("def can_author", 1)[1]
    # The docstring NAMES the three rejected signals, so scanning the raw text
    # fails on the explanation rather than on any code. Strip it first — the
    # same mistake this suite is guarding against, made by the guard itself.
    code = re.sub(r'"""[\s\S]*?"""', "", body, count=1)

    assert "has_studio_write_access" in code

    for wrong in ("is_author_mode", "user_is_staff", '"edx-platform.user_role"'):
        assert wrong not in code, (
            f"{wrong} is not populated on the path this handler runs on — "
            "it was tried and it refused real authors"
        )


def test_the_block_delegates_rather_than_reimplementing_the_check():
    """The block must ASK the adapter, not grow its own copy of the decision.

    Without this, the next person needing an authoring check adds four lines to
    the block, contract 1 goes red again, and the fix that was made here is
    undone by someone who never read it."""
    src = BLOCK_SRC.read_text(encoding="utf-8")
    body = src.split("def _can_author", 1)[1].split("\n    def ", 1)[0]
    code = re.sub(r'"""[\s\S]*?"""', "", body, count=1)

    assert "studio_authz import can_author" in code
    assert "has_studio_write_access" not in code


def test_the_platform_import_stays_out_of_lms_startup():
    """Two modules are imported during LMS startup — the block, and anything it
    imports at module scope. Neither may pull in Django auth or edx-platform
    internals (Principle 8).

    The adapter is checked as well as the block: moving the import into a module
    that the block imports at TOP level would satisfy contract 1 and still drag
    edx-platform into every boot, which is the failure this pins."""
    src = BLOCK_SRC.read_text(encoding="utf-8")
    header = src.split("class CourseMateTutorXBlock", 1)[0]
    assert "student.auth" not in header
    assert "get_user_model" not in header
    assert "studio_authz" not in header, (
        "the adapter must be imported inside _can_author, not at module scope"
    )

    # And the adapter keeps its own imports lazy, so importing it is free.
    authz = AUTHZ_SRC.read_text(encoding="utf-8")
    authz_header = authz.split("def can_author", 1)[0]
    assert "student.auth" not in authz_header
    assert "get_user_model" not in authz_header
