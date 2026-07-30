"""Structured reporting for architecture probes.

Every probe emits the same eight sections so the output is *citable* in an
architecture document rather than being a setup log:

    Objective · Method · Commands · Sources · Evidence · Conclusion ·
    Implications · Assumptions & limitations

Two design choices worth stating, because they are what make the output evidence
rather than assertion:

1. **Evidence is captured, never retyped.** `Finding.evidence()` records the
   actual value read from the running system. If a probe cannot obtain a value it
   records the failure as evidence too — an absent result is a finding.

2. **Conclusions carry a confidence.** `CONFIRMED` means observed directly on this
   instance. `INFERRED` means derived from source we read but did not execute.
   `UNVERIFIED` means we are repeating a claim. Mixing these is how a design ends
   up asserting things nobody checked, which is the specific failure the
   CourseMate design document keeps auditing itself for.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Confidence(StrEnum):
    CONFIRMED = "CONFIRMED"      # observed on this running instance
    INFERRED = "INFERRED"        # read from source, not executed
    UNVERIFIED = "UNVERIFIED"    # repeated from documentation only


@dataclass
class Contradiction:
    """A place where what we observed disagrees with published documentation.

    Recorded explicitly, with both sides and a citation, because "the docs say X
    but the system does Y" is the most valuable thing a probe can find and the
    easiest thing to lose in a wall of output.
    """

    claim: str          # what the documentation says
    observed: str       # what this instance actually did
    source: str         # where the claim comes from (URL or file path)
    explanation: str    # why the two differ


@dataclass
class Finding:
    probe_id: str
    title: str
    objective: str
    method: str

    commands: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    evidence_lines: list[tuple[str, str]] = field(default_factory=list)
    conclusions: list[tuple[Confidence, str]] = field(default_factory=list)
    implications: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)

    # --- collection ----------------------------------------------------------

    def command(self, cmd: str) -> "Finding":
        self.commands.append(cmd)
        return self

    def source(self, path: str) -> "Finding":
        """A file path in edx-platform, or a URL. Both are citable."""
        self.sources.append(path)
        return self

    def evidence(self, label: str, value: object) -> "Finding":
        self.evidence_lines.append((label, str(value)))
        return self

    def conclude(self, confidence: Confidence, text: str) -> "Finding":
        self.conclusions.append((confidence, text))
        return self

    def implies(self, text: str) -> "Finding":
        """What this means for the AI Tutor specifically — not a general remark."""
        self.implications.append(text)
        return self

    def limitation(self, text: str) -> "Finding":
        self.limitations.append(text)
        return self

    def contradicts(self, **kwargs) -> "Finding":
        self.contradictions.append(Contradiction(**kwargs))
        return self

    # --- rendering -----------------------------------------------------------

    def render(self) -> str:
        out: list[str] = []
        out.append(f"## {self.probe_id} — {self.title}\n")

        out.append("### Objective\n")
        out.append(self.objective.strip() + "\n")

        out.append("### Method\n")
        out.append(self.method.strip() + "\n")

        if self.commands:
            out.append("### Commands executed\n")
            out.append("```bash")
            out.extend(self.commands)
            out.append("```\n")

        if self.sources:
            out.append("### Source locations\n")
            for path in self.sources:
                out.append(f"- `{path}`" if not path.startswith("http") else f"- {path}")
            out.append("")

        out.append("### Evidence\n")
        if self.evidence_lines:
            out.append("| Observation | Value |")
            out.append("|---|---|")
            for label, value in self.evidence_lines:
                clean = value.replace("|", "\\|").replace("\n", " ")
                if len(clean) > 300:
                    clean = clean[:300] + " …"
                out.append(f"| {label} | `{clean}` |")
        else:
            out.append("_No evidence captured — treat this probe as failed._")
        out.append("")

        out.append("### Conclusion\n")
        for confidence, text in self.conclusions:
            out.append(f"- **{confidence}** — {text}")
        out.append("")

        if self.contradictions:
            out.append("### Contradicts published documentation\n")
            for c in self.contradictions:
                out.append(f"**Documented claim:** {c.claim}\n")
                out.append(f"**Observed on this instance:** {c.observed}\n")
                out.append(f"**Source:** {c.source}\n")
                out.append(f"**Why they differ:** {c.explanation}\n")

        if self.implications:
            out.append("### Implications for the AI Tutor\n")
            for text in self.implications:
                out.append(f"- {text}")
            out.append("")

        out.append("### Assumptions and limitations\n")
        if self.limitations:
            for text in self.limitations:
                out.append(f"- {text}")
        else:
            out.append("- None recorded — which is itself suspicious; every probe has a scope.")
        out.append("")
        out.append("---\n")
        return "\n".join(out)


def environment_block() -> str:
    """Provenance for the whole report. A finding without an environment is not
    reproducible, and an irreproducible finding is an opinion."""

    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            ).stdout.strip() or "n/a"
        except Exception:  # noqa: BLE001
            return "n/a"

    rows = [
        ("Captured (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        ("Host platform", platform.platform()),
        ("Python (in container)", platform.python_version()),
    ]
    try:
        import django

        rows.append(("Django", django.get_version()))
    except Exception:  # noqa: BLE001
        pass
    try:
        import openedx_events

        rows.append(("openedx-events", getattr(openedx_events, "__version__", "unknown")))
    except Exception:  # noqa: BLE001
        pass

    lines = ["| Item | Value |", "|---|---|"]
    lines += [f"| {k} | `{v}` |" for k, v in rows]
    return "\n".join(lines) + "\n"
