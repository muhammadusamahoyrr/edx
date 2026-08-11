"""The MCP server's protocol behaviour and its one security property.

The security property is that the offering is pinned and unreachable from a tool
call. Everything else about this server is protocol plumbing — but plumbing that
answers about the wrong course is the worst failure this integration has, because
the instructor would have no way to notice.
"""

from __future__ import annotations

import io
import json

import pytest
from coursemate_service.agents.registry import registry
from coursemate_service.mcp.server import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    MCPServer,
)

OFFERING = "course-v1:OpenedX+DemoX+DemoCourse"
OTHER = "course-v1:OpenedX+DemoX+2027"


@pytest.fixture
def server():
    return MCPServer(OFFERING, "dr-lee")


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def payload(response) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


# --- the pinned offering ---------------------------------------------------


def test_the_offering_comes_from_the_launch_config(server):
    assert server.ctx.claims.offering_id == OFFERING
    assert OFFERING in server.initialize({})["instructions"]


def test_a_tool_call_cannot_name_another_offering(server):
    """Not "denied" — inexpressible. The registry refuses identity fields, so the
    attempt is refused and logged rather than silently corrected."""
    response = server.handle(rpc("tools/call", {
        "name": "get_plan_context", "arguments": {"offering_id": OTHER}
    }))
    assert response["result"]["isError"] is True
    assert "offering_id" in payload(response)["message"]


def test_no_exposed_schema_offers_a_course_scoping_field(server):
    """The structural half: a client's model is never shown a field it could put
    an offering in."""
    for tool in server.tools_list({})["tools"]:
        properties = set(tool["inputSchema"].get("properties", {}))
        assert not (properties & {"offering_id", "course_id", "student_id", "tenant"})


def test_the_exposed_tools_are_the_agents_own(server):
    """Not a parallel surface with its own filters. If these ever diverge, the
    gate and the authz checks diverge with them — the MCP tools would be a second
    implementation nobody audits."""
    exposed = sorted(t["name"] for t in server.tools_list({})["tools"])
    assert exposed == registry.names()


def test_the_surface_stays_read_only(server):
    """§10.6's claim covers this server too. A write tool appearing here would
    end it just as surely as one appearing on the agent."""
    names = [t["name"] for t in server.tools_list({})["tools"]]
    assert not any(n.startswith(("record_", "write_", "set_", "delete_")) for n in names)


# --- protocol --------------------------------------------------------------


def test_initialize_advertises_tools(server):
    result = server.initialize({})
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "coursemate"


def test_a_notification_gets_no_reply(server):
    """A JSON-RPC notification has no id and takes no response — including no
    error response. Some clients read an unexpected reply as the answer to a
    DIFFERENT request, which corrupts the rest of the session."""
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_an_error_not_a_crash(server):
    response = server.handle(rpc("tools/destroy"))
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_a_non_jsonrpc_message_is_rejected(server):
    assert server.handle({"method": "tools/list", "id": 1})["error"]["code"] == INVALID_REQUEST


def test_a_failing_tool_is_a_successful_rpc_with_isError(server):
    """Collapsing the two would make a broken tool look like a broken server, and
    the client would retry the wrong thing."""
    response = server.handle(rpc("tools/call", {"name": "no_such_tool"}))
    assert "error" not in response
    assert response["result"]["isError"] is True


def test_a_handler_exception_does_not_kill_the_session(server, monkeypatch):
    monkeypatch.setattr(
        server, "tools_list", lambda p: (_ for _ in ()).throw(RuntimeError("/data/secret.db"))
    )
    response = server.handle(rpc("tools/list"))
    assert response["error"]["message"] == "internal error"
    assert "secret" not in json.dumps(response)


def test_malformed_json_does_not_end_the_stream(server):
    """One bad line must not take the rest of the session with it."""
    stdin = io.StringIO("{not json\n" + json.dumps(rpc("tools/list", request_id=7)) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)

    lines = [json.loads(x) for x in stdout.getvalue().strip().split("\n")]
    assert lines[0]["error"]["code"] == PARSE_ERROR
    assert lines[1]["id"] == 7
    assert "tools" in lines[1]["result"]


def test_every_response_is_one_flushed_line(server):
    """Line-delimited framing. A response containing a raw newline would be read
    as two messages, and a client blocked on a buffered reply is
    indistinguishable from a hung server."""
    stdin = io.StringIO(json.dumps(rpc("initialize")) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)

    text = stdout.getvalue()
    assert text.endswith("\n")
    assert text.count("\n") == 1
