"""Conversation text never reaches a log line.

§3.1 keeps conversation text with the platform. `boundary/impl.py::_audit` is
where that rule is written down, and it obeys it — *"Deliberately not the
student's question: §3.1 keeps chat text out of our logs, and an audit trail does
not need the content to record that access happened."*

**Two other places produced the data and logged it anyway**, until 2026-08-14:

    api/chat.py     log.info("chat: user=%s ... q=%r", claims.sub, question[:80])
    ai/pipeline.py  log.info("unsupported claim (coverage %.2f): %.80s", ..., sentence)

The first is the worse of the two: a student identifier and what that student
asked, on one line, at INFO, on a shared LMS host. The second logged answer text,
which is derived from the question — so between them the log held both halves of
the conversation.

Neither was a lapse in care. The rule lived in the module that *refused* the data
and nowhere near the modules that *had* it, so obeying it depended on having read
`_audit`'s comment. This test moves the rule to where it can fail.

**A source scan over the AST, not a runtime capture.** A caplog-based test would
only cover the paths a test happens to exercise, and the risk here is a NEW log
line on a path nobody wrote a test for. Walking every `log.*(...)` call in the
package covers the ones that do not exist yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICE_SRC = Path(__file__).resolve().parents[1] / "coursemate_service"

#: Expressions that ARE conversation text. Matched against the source of each
#: argument to a logging call, so `request.question[:80]` and
#: `claim.sentence.strip()` are both caught by their base expression.
#:
#: Attribute names rather than bare identifiers: `answer` alone would flag
#: `answer_parts` and every local called `answer` that holds a count.
FORBIDDEN_IN_LOG_ARGS = (
    "request.question",
    "req.question",
    "claim.sentence",
    "turn.content",
    "frame.text",
    "part.text",
    "chunk.text",
    "c.text",
)

#: Names that hold the assembled answer or the built prompt. Bare identifiers, so
#: they are compared against the WHOLE argument expression rather than searched
#: for inside it — `len(answer)` and `answer_parts` must stay legal.
FORBIDDEN_LOG_ARG_NAMES = frozenset({"answer", "messages", "question", "sentence"})


#: Calls that turn text into a MEASUREMENT of text. `len(request.question)` is
#: the shape the fix uses and must stay legal — the objection is to the content
#: reaching the log, never to its size, which is the operational signal the
#: content was standing in for. Anything here must be one-way: `len` and `hash`
#: are, `repr` and `str` are not.
MEASUREMENTS = frozenset({"len", "hash"})


def _is_measurement(arg: ast.expr) -> bool:
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Name)
        and arg.func.id in MEASUREMENTS
    )


def _log_calls(tree: ast.AST):
    """Every `log.<level>(...)` / `logger.<level>(...)` call in a module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id not in {"log", "logger", "logging"}:
            continue
        if func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
            continue
        yield node


def _modules() -> list[Path]:
    return sorted(SERVICE_SRC.rglob("*.py"))


def test_the_scan_finds_log_calls_at_all():
    """A matcher that finds nothing would make every assertion below vacuous."""
    total = sum(
        len(list(_log_calls(ast.parse(p.read_text(encoding="utf-8")))))
        for p in _modules()
    )
    assert total > 30, f"only {total} log calls found; the matcher has rotted"


@pytest.mark.parametrize(
    "path", _modules(), ids=lambda p: str(p.relative_to(SERVICE_SRC))
)
def test_no_log_call_passes_conversation_text(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for call in _log_calls(tree):
        for arg in call.args[1:]:  # arg 0 is the format string
            if _is_measurement(arg):
                continue
            src = ast.unparse(arg)
            if any(bad in src for bad in FORBIDDEN_IN_LOG_ARGS):
                offenders.append(f"line {call.lineno}: {src}")
            elif isinstance(arg, ast.Name) and arg.id in FORBIDDEN_LOG_ARG_NAMES:
                offenders.append(f"line {call.lineno}: {src}")

    assert not offenders, (
        f"{path.name} logs conversation text: {offenders}. §3.1 keeps it with "
        f"the platform — log a length, a count or a hash instead. If this is a "
        f"false positive, narrow the expression rather than widening the list."
    )


def test_the_chat_route_still_logs_something_useful():
    """The fix must not be 'delete the line'. An access record with no signal is
    a different failure from a record with too much."""
    src = (SERVICE_SRC / "api" / "chat.py").read_text(encoding="utf-8")
    assert "question_chars" in src, "the chat route logs no size signal at all"
    assert "claims.sub" in src, "the chat route no longer records who asked"


def test_the_audit_record_still_refuses_the_question():
    """Where the rule was already obeyed. If `_audit` ever starts taking the
    query, the rule has been lost at its own source."""
    src = (SERVICE_SRC / "boundary" / "impl.py").read_text(encoding="utf-8")
    body = src.split("def _audit", 1)[1].split("\n    def ", 1)[0]
    assert "query" not in body.split('"""')[-1]
