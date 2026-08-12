"""Citation persistence and sanitisation.

`persist_turn` is a handler the student's own page calls, so its body is
**untrusted input** even though the citations originally came from the service.
Whatever is stored here is re-rendered on every subsequent page load, which makes
storage a persistence point for anything the browser sends.
"""

from __future__ import annotations

import pytest
from coursemate_platform.xblock.citations import (
    MAX_CITATIONS_PER_TURN,
    MAX_UNSUPPORTED_PER_TURN,
)
from coursemate_platform.xblock.citations import (
    clean_citations as _clean_citations,
)
from coursemate_platform.xblock.citations import (
    clean_unsupported as _clean_unsupported,
)


def test_valid_citations_survive():
    out = _clean_citations([
        {"usage_key": "block-v1:X+type@html+block@a",
         "display_name": "Transcripts",
         "url": "/courses/c/jump_to/block-v1:X+type@html+block@a"},
    ])
    assert len(out) == 1
    assert out[0]["display_name"] == "Transcripts"
    assert out[0]["url"].startswith("/courses/")


def test_only_whitelisted_fields_are_stored():
    """Storing the payload verbatim would put arbitrary browser-supplied structure
    into user_state and re-render it on every load."""
    out = _clean_citations([{
        "usage_key": "block-v1:X",
        "display_name": "Lesson",
        "url": "/ok",
        "evil": "<script>alert(1)</script>",
        "nested": {"more": "junk"},
    }])
    assert set(out[0]) == {"usage_key", "display_name", "url"}


def test_citation_without_a_usage_key_is_dropped():
    """usage_key is what makes a citation checkable. Without it there is nothing
    to verify the link against."""
    assert _clean_citations([{"display_name": "No key", "url": "/x"}]) == []


def test_count_is_capped():
    """A retrieval returning many chunks must not grow a student's user_state
    without bound."""
    many = [{"usage_key": f"block-{i}", "display_name": f"L{i}", "url": "/x"}
            for i in range(50)]
    assert len(_clean_citations(many)) == MAX_CITATIONS_PER_TURN


def test_field_lengths_are_bounded():
    out = _clean_citations([{
        "usage_key": "k" * 5000, "display_name": "d" * 5000, "url": "/" + "u" * 5000,
    }])
    assert len(out[0]["usage_key"]) <= 255
    assert len(out[0]["display_name"]) <= 200
    assert len(out[0]["url"]) <= 500


@pytest.mark.parametrize("junk", [None, "a string", 42, {"not": "a list"}, [1, 2, 3], ["x"]])
def test_malformed_payloads_never_raise(junk):
    """persist_turn must not 500 on a malformed body — it is called by a browser
    after every answer, and an exception there loses the turn."""
    assert isinstance(_clean_citations(junk), list)


def test_javascript_url_is_stored_but_neutralised_at_render():
    """Defence in depth, stated honestly.

    Sanitising here does NOT reject a javascript: URL — the value is bounded and
    whitelisted, not scheme-checked. The client's safeHref() is what refuses to
    render it, and that is the correct place: the same check protects live frames
    from the service, which never pass through this function at all.
    """
    out = _clean_citations([{"usage_key": "k", "display_name": "d",
                             "url": "javascript:alert(1)"}])
    assert out[0]["url"] == "javascript:alert(1)"  # stored verbatim, by design


# --- unsupported-claim marks (D1) ------------------------------------------
#
# Same trust boundary as the citations above, and a sharper reason to persist
# them. Marks used to live only for the length of the live stream, so a refresh
# dropped the warning and kept the sentence — a reloaded answer read as MORE
# trustworthy than the one the student had just been shown.


def test_valid_marks_survive():
    assert _clean_unsupported(["Deadlock cannot occur here.", "  Trimmed.  "]) == [
        "Deadlock cannot occur here.",
        "Trimmed.",
    ]


def test_marks_are_capped():
    out = _clean_unsupported([f"sentence {i}" for i in range(50)])
    assert len(out) == MAX_UNSUPPORTED_PER_TURN


def test_mark_length_is_bounded():
    out = _clean_unsupported(["x" * 5000])
    assert len(out[0]) == 500


def test_non_strings_are_dropped_not_coerced():
    """Coercing would store `"{'a': 1}"` as a sentence and render it to the
    student. Dropping keeps whatever the browser invented out of `user_state`."""
    assert _clean_unsupported(["real", {"a": 1}, None, 42, ["nested"]]) == ["real"]


def test_empty_and_whitespace_marks_are_dropped():
    assert _clean_unsupported(["", "   ", "\n\t"]) == []


@pytest.mark.parametrize("junk", [None, "string", 42, {"a": 1}, object()])
def test_malformed_mark_payloads_never_raise(junk):
    """This runs after every answer. An exception here loses the turn."""
    assert _clean_unsupported(junk) == []


def test_marks_are_stored_as_text_never_as_markup():
    """The renderer uses textContent, so this is belt-and-braces — but the value
    is browser-supplied and lands in per-student storage, so it must not be
    silently transformed on the way in either."""
    out = _clean_unsupported(["<script>alert(1)</script>"])
    assert out == ["<script>alert(1)</script>"]
