"""Routes that do blocking I/O must be `def`, never `async def`.

FastAPI runs a plain `def` endpoint in a threadpool and an `async def` one
**directly on the event loop**. Every route in `api/ingest.py`, plus `load_pack`
and `invalidate`, calls synchronous SQLite or synchronous Redis. Declared
`async`, they ran a full course reindex — hundreds of inserts, a verify, and a
swap holding `BEGIN IMMEDIATE` — on the same loop streaming answers to students.
One bootstrap stalled every open stream in the process.

**The project already had the rule and applied it on the read side only.**
`ai/retrieval.py` wraps retrieval in `asyncio.to_thread` and says why: *"one slow
query must not stall every other student's stream."* The write path was declared
`async` and awaited nothing.

That is why this is a test and not a comment. An `async def` that never awaits is
indistinguishable from a correct one at a glance, it reads as more modern than
the `def` that is right, and the next person adding a route here will copy the
one above it. The failure is invisible in every test that calls the route
directly — it only appears as latency on a *different* request, under
concurrency, in production.

**Deliberately a source scan, not a runtime check.** `inspect.iscoroutinefunction`
would be the obvious tool and it inspects the decorated object, which is a step
removed from the line someone writes. A grep fails on the thing that is typed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICE_SRC = Path(__file__).resolve().parents[1] / "coursemate_service"

#: Modules whose every route touches blocking SQLite or blocking Redis.
#: `ingest.py` is the whole write path; `packs.py` loads a past-paper pack in one
#: transaction; `invalidation.py` runs a Redis SCAN + DELETE over the keyspace,
#: which for a course-wide notice is unbounded work.
BLOCKING_MODULES = ["api/ingest.py", "api/packs.py", "api/invalidation.py"]

#: Routes allowed to stay `async def` because they really do await. `chat` and
#: `practice_stream` return a StreamingResponse over an async generator, and
#: `study_plan` awaits `asyncio.to_thread` — which is the correct shape and the
#: one the blocking routes should NOT imitate by adding `async` without it.
ASYNC_IS_CORRECT = {"chat", "whoami", "practice_stream", "study_plan", "plan", "status"}


def _routes(module_path: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level functions carrying an `@router.<method>` decorator."""
    tree = ast.parse((SERVICE_SRC / module_path).read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name):
                if call.value.id == "router":
                    found.append(node)
                    break
    return found


@pytest.mark.parametrize("module_path", BLOCKING_MODULES)
def test_the_scan_finds_routes_at_all(module_path):
    """A decorator match that finds nothing would make the test below vacuous —
    which is the failure shape this repository keeps finding, so it is checked
    rather than assumed."""
    assert _routes(module_path), f"no routes found in {module_path}; the scan has rotted"


@pytest.mark.parametrize("module_path", BLOCKING_MODULES)
def test_blocking_routes_are_not_declared_async(module_path):
    offenders = [
        node.name
        for node in _routes(module_path)
        if isinstance(node, ast.AsyncFunctionDef)
    ]
    assert not offenders, (
        f"{module_path}: {offenders} are `async def` and call blocking SQLite or "
        f"Redis, so they run on the event loop and stall every concurrent "
        f"student stream. Drop `async` — FastAPI will run them in a threadpool — "
        f"or await the work through asyncio.to_thread."
    )


@pytest.mark.parametrize("module_path", BLOCKING_MODULES)
def test_a_blocking_route_never_awaits(module_path):
    """The other direction, and the reason the fix above is safe.

    Making a route sync is only correct while it awaits nothing. If one of these
    grows a genuine `await`, `def` becomes a syntax error rather than a silent
    regression — but a *nested* async helper would not, so the body is checked."""
    for node in _routes(module_path):
        awaits = [n for n in ast.walk(node) if isinstance(n, ast.Await)]
        assert not awaits, (
            f"{module_path}:{node.name} awaits something, so it cannot be a plain "
            f"`def`. Keep it async and move the BLOCKING call into "
            f"asyncio.to_thread instead."
        )


def test_the_read_path_still_runs_retrieval_off_the_loop():
    """The rule this fix was derived from, pinned where it was already true.

    `CourseContextProvider.fetch` is the reason the read side never had this
    problem. If it ever stops using a worker thread, the chat path acquires the
    defect the ingest path just lost."""
    src = (SERVICE_SRC / "ai" / "retrieval.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(self.fetch_sync" in src
