"""MCP — the same tools, for an instructor's own assistant.

**Local stdio, one offering, instructor-scoped.** No network listener, no
credential, no caller identity. The offering is pinned in the launch config, and
the server speaks JSON-RPC over stdin/stdout to the process that started it.

That shape was chosen over an HTTP MCP server for one reason: the threat model
collapses. An MCP server exposed on a port has a caller identity that does NOT
inherit the boundary's JWT — so cross-course leakage would become something to
check, with a new authn surface, a new rate limiter and a new credential class
behind it. Over local stdio the question does not arise: there is no network
caller to get wrong, and cross-offering access is impossible by construction
because the offering is a constant read from the config, not an argument.

The residual risk is file permissions on the instructor's machine, which is the
same risk their course export already carries.

**This is the designated cut candidate.** It ships last, after the eval work,
because multi-model routing is a Phase 3 non-negotiable and this is not: memory
and the tool registry already satisfy the agentic requirement. It is a thin
adapter over `agents.registry` precisely so dropping it strands nothing — the
tools it exposes are the tools the agent already uses, unchanged.
"""

from __future__ import annotations
