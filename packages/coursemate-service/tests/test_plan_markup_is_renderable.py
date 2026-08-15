"""Every construct `api/plan.py` emits is one the browser can render.

**The loop this closes.** Three layers each assumed another owned presentation:
`api/plan.py` wrote markdown, the contract carried opaque text tokens, and
`tutor.js` assigned them to `textContent`. Nobody owned the decision, so the
markup fell through to the student — `## CLO-1 — …` and
`_Your record: not practised yet._`, 14 of 18 constructs shown literally.

Phase 1 taught the renderer those constructs. This stops the gap reopening from
the OTHER side: a new line in `_render()` using a construct the browser does not
handle would ship raw text again, and no existing test would notice, because the
producer and the renderer are in different packages and different languages.

**Deliberately a source scan across the boundary, like `test_error_contract.py`.**
It reads `tutor.js` as data, by path. That is not an import and does not cross
the boundary `.importlinter` contract 2 defends — the file is read the way a
fixture is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLAN_PY = ROOT / "packages" / "coursemate-service" / "coursemate_service" / "api" / "plan.py"
TUTOR_JS = (
    ROOT / "packages" / "coursemate-platform" / "coursemate_platform"
    / "xblock" / "static" / "js" / "src" / "tutor.js"
)

#: Constructs the renderer handles, and the evidence in `tutor.js` that it does.
#: Keyed by the marker as it appears in a plan line.
RENDERABLE = {
    "## heading": "HEADING",
    "- bullet": "BULLET",
    "whole-line italic": "LINE_ITALIC",
    "indented sub-line": "INDENTED",
    "**bold**": "BOLD",
    "1. ordered": "ORDERED",
}

#: What `_render()` and the plan header may contain. Each entry is a pattern
#: that finds the construct in emitted text, plus the RENDERABLE key that must
#: cover it.
CONSTRUCTS = [
    (r"^#{1,3} ",        "## heading"),
    (r"^- ",             "- bullet"),
    (r"^_.+_$",          "whole-line italic"),
    (r"^\s{2,}\S",       "indented sub-line"),
    (r"\*\*[^*]+\*\*",   "**bold**"),
    (r"^\d+\. ",         "1. ordered"),
]

#: Markers that would NOT survive the renderer. If `plan.py` ever emits one, the
#: student sees it raw — which is the whole defect this file exists for.
UNRENDERABLE = [
    (r"^```",            "fenced code block"),
    (r"^\| .* \|",       "table row"),
    (r"\[[^\]]+\]\([^)]+\)", "markdown link"),
    (r"^> ",             "block quote"),
]


def plan_source() -> str:
    return PLAN_PY.read_text(encoding="utf-8")


def emitted_string_literals() -> list[str]:
    """Every string literal in `plan.py` that could reach a TOKEN frame.

    Crude on purpose. It over-collects — docstrings and log messages come too —
    and over-collecting is the safe direction: a false alarm is a line someone
    reads, a miss is markup on a student's screen.
    """
    src = plan_source()
    out: list[str] = []
    for m in re.finditer(r'f?"([^"\\\n]*(?:\\.[^"\\\n]*)*)"', src):
        out.append(m.group(1))
    for m in re.finditer(r"f?'([^'\\\n]*(?:\\.[^'\\\n]*)*)'", src):
        out.append(m.group(1))
    return out


def renderer_source() -> str:
    return TUTOR_JS.read_text(encoding="utf-8")


def test_the_scan_finds_the_plan_renderer_at_all():
    """A scan that matches nothing would make everything below vacuous — the
    failure mode this repository keeps finding, so it is checked."""
    src = plan_source()
    assert "def _render(" in src, "plan.py no longer has _render; this file is stale"
    assert "## " in src, "plan.py emits no headings; the fixture assumptions have moved"


@pytest.mark.parametrize("construct,marker", list(RENDERABLE.items()))
def test_the_renderer_declares_every_construct_we_claim_it_handles(construct, marker):
    """The right-hand side of the contract. If `tutor.js` drops one of these
    rules, this file's other assertions would silently start passing for the
    wrong reason.

    Matched as a DECLARATION, not a substring. `marker in source` passed when
    `HEADING` was renamed to `HEADING_REMOVED` — the mutation that was supposed
    to prove this test works. A substring check cannot tell a rule from a
    renamed corpse of one.
    """
    assert re.search(rf"\bvar {marker}\s*=", renderer_source()), (
        f"tutor.js no longer declares `var {marker} =`, so plan markup for "
        f"'{construct}' would reach the student raw"
    )


def test_every_construct_the_plan_emits_is_renderable():
    """The load-bearing check, in the direction that actually breaks.

    A new line in `_render()` using an unhandled construct is invisible to every
    other test: the service tests assert on text, and the browser tests use
    their own fixtures. Only a comparison across the two catches it."""
    literals = emitted_string_literals()
    assert literals, "no string literals found in plan.py; the scan has rotted"

    js = renderer_source()

    def handled(marker: str) -> bool:
        # Declaration, not substring - see the note on the parametrised test.
        return bool(re.search(rf"var {marker}\s*=", js))
    unhandled: list[str] = []
    for text in literals:
        for pattern, construct in CONSTRUCTS:
            if re.search(pattern, text, re.MULTILINE):
                if not handled(RENDERABLE[construct]):
                    unhandled.append(f"{construct!r} in {text[:48]!r}")
    assert not unhandled, (
        "plan.py emits markup the browser cannot render: "
        + "; ".join(unhandled)
    )


def test_the_plan_emits_nothing_the_renderer_cannot_handle():
    """The forward-looking half. These constructs have no rule in `tutor.js` and
    would render as literal text, so `_render()` must not start using one."""
    offenders: list[str] = []
    for text in emitted_string_literals():
        for pattern, name in UNRENDERABLE:
            if re.search(pattern, text, re.MULTILINE):
                offenders.append(f"{name} in {text[:48]!r}")
    assert not offenders, (
        "plan.py emits a construct the renderer has no rule for, so it will "
        "reach the student as raw text: " + "; ".join(offenders)
    )


def test_the_source_line_stays_attached_to_its_question():
    """§7.6 wants provenance on every item. `_render` indents it under the
    bullet, and the renderer only keeps that association because of the
    INDENTED rule — if either side changes alone, the source line detaches and
    reads as applying to the whole list."""
    src = plan_source()
    assert '"  _Source:' in src or "'  _Source:" in src or '  _Source: ' in src, (
        "plan.py no longer indents the source line under its question"
    )
    assert "INDENTED" in renderer_source(), (
        "the renderer no longer attaches indented lines to their list item"
    )
