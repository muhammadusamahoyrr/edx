"""MCP stdio server — JSON-RPC 2.0 over stdin/stdout.

    python -m coursemate_service.mcp.server --offering course-v1:Org+Course+Run

Hand-rolled rather than built on an SDK, and that is a deliberate trade. The
protocol surface actually needed here is three methods — `initialize`,
`tools/list`, `tools/call` — over line-delimited JSON. An SDK would add a
dependency to the service image, to the `.importlinter` graph and to the
security review, in exchange for code we would still have to read. The framing is
the simplest thing that works with a local client, and it is testable without a
subprocess.

**Every tool is the agent's own tool**, taken from `agents.registry` unchanged.
Not re-implemented, not a parallel surface with its own filters. So the guarantees
hold identically: the confidence gate runs per call, model-supplied identity is
refused, arguments are validated against the same strict schemas, and the tool
surface stays read-only.

**The offering is pinned at launch and is not an argument.** It is injected into
the `ToolContext` alongside synthetic instructor claims. A `tools/call` that names
an offering is refused by the registry like any other identity attempt, so
cross-course access is not merely denied — it cannot be expressed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any, TextIO

from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims

from ..agents import tools as _tools  # noqa: F401 — registers the tool surface
from ..agents.registry import ToolContext, ToolStatus, registry

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "coursemate", "version": "0.1.0"}

#: JSON-RPC 2.0 reserved codes. Spelled out rather than inlined so a wrong one is
#: visible: a client distinguishes "you sent nonsense" from "I broke", and
#: collapsing them turns the second into the first.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def instructor_claims(offering_id: str, username: str) -> StudentClaims:
    """Identity for the local operator.

    Synthetic, and deliberately so: there is no caller to authenticate over
    stdio, and inventing a token exchange would add an authn surface to buy
    nothing. What it is NOT is a bypass — the offering is fixed at launch, so
    these claims can only ever scope to the one course whoever started the
    process already had the file permissions to read.

    `enforce_enrollment` still applies. On a deployment with it on, an instructor
    who is not enrolled in the offering they pinned gets an authorization error
    from the boundary, which is correct: the platform decides entitlement here
    exactly as it does for a student.
    """
    now = int(time.time())
    return StudentClaims(
        sub=f"mcp:{username}", username=username,
        course_id=offering_id, offering_id=offering_id,
        roles=["instructor"], aud=AUDIENCE_STUDENT, exp=now + 86400, iat=now,
    )


class MCPServer:
    def __init__(self, offering_id: str, username: str) -> None:
        self.offering_id = offering_id
        self.ctx = ToolContext(claims=instructor_claims(offering_id, username))

    # --- methods ---------------------------------------------------------

    def initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            # Surfaced in the handshake so the operator's client shows which
            # course it is pinned to. A server that silently answered about a
            # different course than the instructor believed would be the worst
            # possible failure of this integration.
            "instructions": (
                f"Read-only access to CourseMate for {self.offering_id}. "
                "Course content is returned only when it passes the same "
                "confidence gate the student tutor uses."
            ),
        }

    def tools_list(self, params: dict) -> dict:
        return {
            "tools": [
                {
                    "name": s["name"],
                    "description": s["description"],
                    # MCP's key is `inputSchema`; the registry now emits OpenAI's
                    # `parameters`. Mapped here rather than making the registry
                    # speak two dialects.
                    "inputSchema": s["parameters"],
                }
                for s in registry.schemas()
            ]
        }

    def tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        result = registry.invoke(name, params.get("arguments"), self.ctx)

        payload: dict[str, Any] = {"status": result.status.value}
        if result.data is not None:
            payload["data"] = result.data
        if result.message:
            payload["message"] = result.message

        return {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, default=str)}],
            # MCP's own error channel, distinct from a JSON-RPC error. A tool that
            # failed is a successful RPC carrying a failed result — collapsing the
            # two would make a broken tool look like a broken server, and the
            # client would retry the wrong thing.
            "isError": result.status is ToolStatus.ERROR,
        }

    # --- dispatch --------------------------------------------------------

    def handle(self, request: dict) -> dict | None:
        """One JSON-RPC request. Returns None for a notification.

        Never raises: an exception here would kill the server for the rest of the
        session over one malformed message.
        """
        request_id = request.get("id")
        method = request.get("method")

        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")

        # A notification has no id and takes no reply — including no error reply.
        # Answering one is a protocol violation that some clients treat as a
        # response to a *different* request, which corrupts the whole session.
        if request_id is None:
            return None

        handler = {
            "initialize": self.initialize,
            "tools/list": self.tools_list,
            "tools/call": self.tools_call,
        }.get(method)
        if handler is None:
            return _error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

        try:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": handler(request.get("params") or {})}
        except Exception as exc:
            # The exception text is not forwarded: it can carry paths and SQL. The
            # operator gets the traceback on stderr, the client gets a fact.
            log.exception("mcp method %s raised %s", method, type(exc).__name__)
            return _error(request_id, INTERNAL_ERROR, "internal error")

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        """Line-delimited JSON, one request per line."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                _write(stdout, _error(None, PARSE_ERROR, "invalid JSON"))
                continue
            if not isinstance(request, dict):
                _write(stdout, _error(None, INVALID_REQUEST, "expected an object"))
                continue
            response = self.handle(request)
            if response is not None:
                _write(stdout, response)


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    # Unbuffered by line: a client blocked waiting for a reply that is sitting in
    # our buffer is indistinguishable from a hung server.
    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offering", required=True,
                        help="the ONE offering this server may read. Not an argument "
                             "any tool call can change.")
    parser.add_argument("--user", default="instructor",
                        help="recorded in the audit log for every tool call")
    args = parser.parse_args(argv)

    # stderr, never stdout: stdout is the protocol channel, and a stray log line
    # there is a parse error at the client.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    log.info("coursemate mcp: stdio, pinned to %s", args.offering)

    MCPServer(args.offering, args.user).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
