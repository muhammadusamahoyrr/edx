"""The Studio panel's index-status line.

This exists because the line was hardcoded. `studio_view.html` shipped the
literals "never" and "0", and `studio_view()` never read `CourseIndexState` at
all — so a course with a completed bootstrap still reported that it had never
been indexed, on every page load, until someone clicked the button again. Found
by screenshotting the panel for a course with 55 live chunks (2026-08-13).

The shape of that failure is the one this project keeps hitting: a display that
is internally consistent and disconnected from the world. So both halves are
pinned here — the read, and the template that renders it. Testing only the read
would leave the bug reachable, because the bug WAS the template.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from coursemate_platform.xblock.tutor_block import CourseMateTutorXBlock

COURSE = "course-v1:OpenedX+DemoX+DemoCourse"

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "coursemate_platform" / "xblock" / "static" / "html" / "studio_view.html"
)


class _Stub:
    """Enough of the block for `_index_status`, per this suite's convention."""

    def __init__(self, course_id=COURSE):
        self._cid = course_id

    def _course_id(self):
        return self._cid


def _status(stub):
    return CourseMateTutorXBlock._index_status(stub)


def _render(context: dict) -> str:
    """Render the real template file, without needing settings.TEMPLATES.

    A standalone `Engine` is used rather than the XBlock loader because the
    loader pulls in the resource machinery; the file on disk is the thing under
    test either way.
    """
    from django.template import Context, Engine

    return Engine().from_string(TEMPLATE.read_text(encoding="utf-8")).render(
        Context(context)
    )


# --- the read -------------------------------------------------------------


def test_no_row_reads_never_and_zero(django_db):
    assert _status(_Stub()) == {"last_indexed": "never", "block_count": 0}


def test_reading_a_missing_course_does_not_create_a_row(django_db):
    """Rendering a config panel must not write bootstrap-progress state.

    `CourseIndexState.for_course` get_or_creates, which is right for the task and
    wrong for a GET. A conjured row also makes the reconciliation sweep's view of
    "courses someone tried to index" untrue.
    """
    from coursemate_platform.models import CourseIndexState

    _status(_Stub("course-v1:OpenedX+NeverTouched+2026"))
    assert not CourseIndexState.objects.filter(
        course_id="course-v1:OpenedX+NeverTouched+2026"
    ).exists()


def test_completed_bootstrap_is_reported(django_db):
    from coursemate_platform.models import CourseIndexState
    from django.utils import timezone

    when = timezone.now()
    CourseIndexState.objects.create(
        course_id=COURSE, block_count=222, blocks_indexed=222, last_indexed_at=when
    )

    got = _status(_Stub())
    assert got["block_count"] == 222
    assert got["last_indexed"] == timezone.localtime(when).strftime("%Y-%m-%d %H:%M")
    assert got["last_indexed"] != "never"


def test_state_row_with_no_completed_run_still_reads_never(django_db):
    """A row exists as soon as a bootstrap STARTS. Started is not indexed."""
    from coursemate_platform.models import CourseIndexState

    CourseIndexState.objects.create(course_id=COURSE, block_count=222, run_id="abc")
    assert _status(_Stub())["last_indexed"] == "never"


def test_status_is_scoped_to_this_course(django_db):
    """A neighbouring course's index must not be reported as this one's."""
    from coursemate_platform.models import CourseIndexState
    from django.utils import timezone

    CourseIndexState.objects.create(
        course_id="course-v1:OpenedX+OTHER+2026",
        block_count=99,
        last_indexed_at=timezone.now(),
    )
    assert _status(_Stub()) == {"last_indexed": "never", "block_count": 0}


# --- the template ---------------------------------------------------------


def test_template_renders_the_values_it_is_given():
    html = _render(
        {"display_name": "CourseMate Tutor", "enabled": True, "mode": "direct",
         "last_indexed": "2026-08-05 10:07", "block_count": 222}
    )
    assert "2026-08-05 10:07" in html
    assert "222" in html


def test_template_puts_them_in_the_spans_studio_js_updates():
    """studio.js writes into `.cm-last-indexed` and `.cm-block-count` after a
    click. If the initial value renders anywhere else, the panel disagrees with
    itself the moment the button is used."""
    import re

    html = _render(
        {"display_name": "x", "enabled": True, "mode": "direct",
         "last_indexed": "2026-08-05 10:07", "block_count": 222}
    )
    assert re.search(
        r'class="cm-last-indexed"\s*>\s*2026-08-05 10:07\s*<', html
    ), html
    assert re.search(r'class="cm-block-count"\s*>\s*222\s*<', html), html


@pytest.mark.parametrize("literal", ["never", "0"])
def test_template_no_longer_hardcodes_the_status(literal):
    """The regression itself: rendering a real status must not leave the old
    placeholder text behind."""
    html = _render(
        {"display_name": "x", "enabled": True, "mode": "direct",
         "last_indexed": "2026-08-05 10:07", "block_count": 222}
    )
    assert f">{literal}<" not in html


def test_never_still_renders_when_that_is_the_truth():
    html = _render(
        {"display_name": "x", "enabled": True, "mode": "direct",
         "last_indexed": "never", "block_count": 0}
    )
    assert ">never<" in html and ">0<" in html
