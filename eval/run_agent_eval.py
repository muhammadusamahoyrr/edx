"""Agent evaluation — the three regression gates from the design pass.

    python eval/run_agent_eval.py            # replay mode (no provider needed)
    python eval/run_agent_eval.py --live     # also measures tool selection

**What this measures, and what it does not.** The gates are about the LOOP's
decision rules — what the agent does when a tool refuses, fails, or comes back
empty — and those are decided entirely by tool outcomes and counters. A real
model contributes nothing to them, so replay mode runs the *real* `ExamPrepAgent`
against a scripted provider and measures them exactly.

Tool-SELECTION accuracy is different: which tool a real model reaches for first
is the model's behaviour, and a stub measuring it would be measuring the stub.
So it is reported as **NOT MEASURED** unless `--live` is given and a provider is
configured. That distinction is the whole reason this file exists rather than a
single number — §11.1's argument is that measuring only the final answer hides
exactly this kind of gap.

The gates, and each one is a claim made elsewhere in the repo that would
otherwise decay:

    no_cross_offering                 no denied call ever returns content
    no_confident_answer_over_failure  an unrecovered tool failure is always said
    gate_abstains_the_turn            the confidence gate abstains, conservatively
    empty_is_an_answer                an empty result is never a failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "coursemate-service"))
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))

# `Settings` has no defaults for the credentials, on purpose — a deployment must
# not be able to start unconfigured and silently accept unsigned tokens. Replay
# mode signs nothing and reads no index, so obvious placeholders let this run on a
# developer's machine. `setdefault`, so a real environment always wins and a
# `--live` run inside the service container uses the deployment's own values.
for _key, _placeholder in (
    ("COURSEMATE_JWT_SIGNING_KEY", "agent-eval-not-a-real-secret-32-bytes+"),
    ("COURSEMATE_SERVICE_CREDENTIAL", "agent-eval-not-a-real-credential-32b+"),
    ("COURSEMATE_INDEX_PATH", ":memory:"),
    ("COURSEMATE_EXAMPREP_PATH", ":memory:"),
):
    os.environ.setdefault(_key, _placeholder)

from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import FrameType
from coursemate_contracts.errors import ErrorCode
from coursemate_contracts.examprep import ExamPrepRequest

DATASET = Path(__file__).parent / "datasets" / "agent_gold.yaml"


# --- dataset ---------------------------------------------------------------


def load_cases(path: Path) -> tuple[str, list[dict]]:
    """Same minimal YAML subset as `run_eval.py`, and for the same reason: the
    harness runs inside the service image, and a YAML dependency there would be
    a container rebuild for no gain."""
    text = path.read_text(encoding="utf-8")
    offering = re.search(r'^offering_id:\s*"([^"]+)"', text, re.MULTILINE).group(1)

    cases: list[dict] = []
    current: dict | None = None
    in_tools = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("- id:"):
            if current:
                cases.append(current)
            current = {"id": line.split(":", 1)[1].strip(), "tools": [],
                       "expect_frames": [], "forbid_frames": []}
            in_tools = False
        elif current is None:
            continue
        elif line.startswith("request:"):
            current["request"] = line.split(":", 1)[1].strip().strip('"')
            in_tools = False
        elif line.startswith("gate:"):
            current["gate"] = line.split(":", 1)[1].strip().strip('"')
            in_tools = False
        elif line.startswith("expect:"):
            current["expect"] = line.split(":", 1)[1].strip()
            in_tools = False
        elif line.startswith(("expect_frames:", "forbid_frames:")):
            key, body = line.split(":", 1)
            current[key] = [x.strip() for x in body.strip().strip("[]").split(",") if x.strip()]
            in_tools = False
        elif line.startswith("tools:"):
            in_tools = True
        elif in_tools and line.startswith("- name:"):
            current["tools"].append({"name": line.split(":", 1)[1].strip()})
        elif in_tools and line.startswith("outcome:") and current["tools"]:
            current["tools"][-1]["outcome"] = line.split(":", 1)[1].strip()
    if current:
        cases.append(current)
    return offering, cases


# --- scripted tool outcomes ------------------------------------------------


def make_result(name: str, outcome: str):
    """One `ToolResult` per gold outcome word.

    Built from the REAL `ToolResult` type, so a change to its fields breaks this
    harness rather than letting the gold set drift away from the code.
    """
    from coursemate_service.agents.registry import ToolResult, ToolStatus

    if outcome == "ok":
        payload = {"chunks": [{"label": 1, "usage_key": "block-v1:eval",
                               "display_name": "Lesson", "text": "content"}]} \
            if name == "search_course_content" else {"clos": [{"clo_id": "CLO-1"}]}
        return ToolResult(tool=name, status=ToolStatus.OK, data=payload)
    if outcome == "empty":
        return ToolResult(tool=name, status=ToolStatus.OK,
                          data={"clos": [], "mastery": [], "questions": [],
                                "mastery_known": True, "past_papers_available": False},
                          message="Nothing matched.")
    if outcome == "gated":
        # `gate_applied` only for the confidence-gated tool — the distinction the
        # abstain rule turns on.
        return ToolResult(tool=name, status=ToolStatus.GATED, data={"chunks": []},
                          gate_applied=name == "search_course_content")
    if outcome == "denied":
        return ToolResult(tool=name, status=ToolStatus.GATED, data={"chunks": []},
                          message="Not available for this student's enrollment.",
                          gate_applied=name == "search_course_content")
    if outcome == "error":
        return ToolResult(tool=name, status=ToolStatus.ERROR, message="the tool failed")
    if outcome == "identity_attempt":
        return ToolResult(tool=name, status=ToolStatus.ERROR,
                          message="Refused: student_id cannot be supplied.")
    raise ValueError(f"unknown outcome {outcome!r} in the gold set")


class ScriptedRouter:
    """A provider that asks for exactly the case's tools, then answers."""

    def __init__(self, tool_names: list[str]):
        self.pending = list(tool_names)
        self.planning_calls = 0

    async def acompletion(self, *, model, messages, stream=False, **kw):
        if stream:
            return self._stream()
        self.planning_calls += 1
        if not self.pending:
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=[]))])
        name = self.pending.pop(0)
        call = SimpleNamespace(function=SimpleNamespace(name=name, arguments="{}"))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]))])

    async def _stream(self):
        yield SimpleNamespace(model="scripted", choices=[SimpleNamespace(
            delta=SimpleNamespace(content="Here is the plan."), finish_reason="stop")])


# --- running ---------------------------------------------------------------


def _claims(offering: str) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="1", username="admin", course_id=offering, offering_id=offering,
        roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 900, iat=now,
    )


async def run_case(case: dict, offering: str) -> dict:
    from coursemate_service.agents import runner as r

    outcomes = list(case["tools"])
    router = ScriptedRouter([t["name"] for t in outcomes])
    queue = [make_result(t["name"], t["outcome"]) for t in outcomes]

    real_invoke, real_schemas, real_router = (
        r.registry.invoke, r.registry.schemas, r.get_router
    )
    r.registry.invoke = lambda name, args, ctx: queue.pop(0) if queue else make_result(name, "empty")
    r.registry.schemas = list
    r.get_router = lambda: router
    try:
        frames = [f async for f in r.ExamPrepAgent().stream(
            ExamPrepRequest(request=case["request"]), _claims(offering)
        )]
    finally:
        r.registry.invoke, r.registry.schemas, r.get_router = (
            real_invoke, real_schemas, real_router
        )

    kinds = {f.type.value for f in frames}
    last = frames[-1]
    if last.type is FrameType.ERROR:
        got = "abstain" if last.error_code is ErrorCode.ABSTAINED else last.error_code.value
    else:
        got = "answer"

    problems = []
    if got != case["expect"]:
        problems.append(f"expected {case['expect']}, got {got}")
    for required in case["expect_frames"]:
        if required not in kinds:
            problems.append(f"missing required frame {required}")
    for forbidden in case["forbid_frames"]:
        if forbidden in kinds:
            problems.append(f"forbidden frame {forbidden} present")

    return {"id": case["id"], "gate": case.get("gate", "—"), "got": got,
            "frames": sorted(kinds), "problems": problems}


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--live", action="store_true",
                        help="also measure tool-selection accuracy against a real provider")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    offering, cases = load_cases(args.dataset)
    results = [await run_case(c, offering) for c in cases]

    gates: dict[str, list[dict]] = {}
    for row in results:
        gates.setdefault(row["gate"], []).append(row)

    failed = [r for r in results if r["problems"]]
    report = {
        "n_cases": len(results),
        "passed": len(results) - len(failed),
        "gates": {g: all(not r["problems"] for r in rows) for g, rows in gates.items()},
        "tool_selection_accuracy": None,
        "cases": results,
    }

    if args.live:
        report["tool_selection_accuracy"] = await _live_tool_selection(offering, cases)

    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if failed else 0

    print("=" * 78)
    print(f"AGENT EVAL — {len(results)} cases, {len(results) - len(failed)} passed")
    print("=" * 78)
    for gate, rows in sorted(gates.items()):
        ok = all(not r["problems"] for r in rows)
        print(f"  {'PASS' if ok else 'FAIL'}  {gate:34s} ({len(rows)} cases)")
    if failed:
        print("\nFAILURES")
        for row in failed:
            print(f"  {row['id']}  {'; '.join(row['problems'])}")
            print(f"        frames: {', '.join(row['frames'])}")

    acc = report["tool_selection_accuracy"]
    print()
    print("  tool-selection accuracy : "
          + ("NOT MEASURED — needs a configured provider (--live); "
             "a stub measuring this would measure the stub"
             if acc is None else f"{acc:.2f}"))
    print("  NOT measured here: whether the plan is pedagogically good. That is")
    print("  the Feature B rubric (eval/feature_b_rubric.py) and a human rater.")
    return 1 if failed else 0


async def _live_tool_selection(offering: str, cases: list[dict]) -> float | None:
    """Which tool a real model reaches for first, against the gold's first tool.

    Returns None — never a number — when no provider is configured. Reporting 0.0
    would read as "the model chose badly" when the truth is "nothing was asked".
    """
    from coursemate_service.agents import runner as r
    from coursemate_service.ai.client import NoModelConfigured

    try:
        r.get_router()
    except NoModelConfigured:
        print("  (--live given, but no provider is configured; skipping)", file=sys.stderr)
        return None

    scored = [c for c in cases if c["tools"]]
    if not scored:
        return None
    hits = 0
    for case in scored:
        seen: list[str] = []
        real_invoke = r.registry.invoke

        def spy(name, args, ctx, _seen=seen):
            _seen.append(name)
            return make_result(name, "empty")

        r.registry.invoke = spy
        try:
            async for _ in r.ExamPrepAgent().stream(
                ExamPrepRequest(request=case["request"]), _claims(offering)
            ):
                pass
        finally:
            r.registry.invoke = real_invoke
        hits += bool(seen) and seen[0] == case["tools"][0]["name"]
    return hits / len(scored)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
