"""First-turn response cache — design §6.4, and §10.2's warning about it.

The tutor's cost and latency are dominated by one thing: a 55-second provider
call. Retrieval is ~3 ms of local SQLite. So the only cache worth having is one
that skips generation, and the only questions safe to skip it for are the ones
whose answer depends on nothing but the question.

**First turn only.** With `history == []` the answer is a function of (question,
what this caller may retrieve, index version, mode). Add a conversation and it
stops being a function of anything stable — B1/B2 make the retrieval query itself
depend on the previous turn, so two students asking the same follow-up after
different first turns must get different answers. Caching those would serve one
student's conversation to another. The check is `not request.history`, and it is
the first thing both the read and the write do.

**Why this module is here and not in `ai/`.** `.importlinter` contract 3 forbids
`coursemate_service.ai` from importing `coursemate_service.knowledge`, and the
key and policy functions this cache is required to use live under
`knowledge/cache/`. Sitting at the service root — beside `budget.py` and
`shared_state.py`, which the pipeline already imports the same way — keeps the
contract intact without copying a key derivation that must never drift.

**The key was designed before this module existed**, in `knowledge/cache/keys.py`,
with a README that says whoever wires a cache inherits the rules rather than
rediscovering the bugs behind them. This is that inheritance. Nothing here
invents a key; it fills in `response_key`'s parameters.

**Degrades to no cache at all.** No Redis, or Redis failing, means every read is
a miss and every write is dropped. That is the whole policy — a cache is an
optimisation, and `shared_state` already argues that refusing to serve when an
optimisation is down turns it into an outage. Unlike the budget ledger there is
no per-process fallback, because a missed cache costs latency while a missed
charge costs money.
"""

from __future__ import annotations

import json
import logging

from coursemate_contracts.auth import StudentClaims
from coursemate_contracts.chat import ChatRequest

from . import shared_state
from .config import settings
from .knowledge.cache.keys import normalize_query, response_key, scope_of
from .knowledge.cache.policy import PersonalDataNotCacheable, assert_cacheable

log = logging.getLogger(__name__)

#: Bumped when the stored shape changes. An old entry then simply misses rather
#: than being handed to code that expects different fields — cheaper and safer
#: than a migration for data that is regenerable by definition.
PAYLOAD_VERSION = 1


def is_cacheable_request(request: ChatRequest) -> bool:
    """First turn only. Everything else in this module assumes this returned True."""
    return not request.history


def cache_key(request: ChatRequest, claims: StudentClaims, index_version: str) -> str:
    """Fill in the response key the design already specified.

    Every component is either server-derived or verified: `offering_id`, `sub`,
    `roles` and `group_tokens` come from the signed token, `index_version` from
    the boundary, and only the question text comes from the request body — where
    it is normalised, not trusted.

    **`usage_key` is in the filters even though nothing reads it yet.** B1/B2
    accept it and deliberately ignore it, so it does not currently change an
    answer. The day it does, a key that omitted it would serve an answer built
    for one block to a student standing on another — a bug that would look like
    a retrieval defect and be found nowhere near this file. Including it costs a
    hit-rate the deployment does not currently spend, because the XBlock does not
    send the field.
    """
    return response_key(
        tenant=settings.tenant,
        offering_id=claims.offering_id,
        course_version=index_version,
        effective_scope=scope_of(
            student_id=claims.sub,
            roles=claims.roles,
            enrolled_offerings=[claims.offering_id],
        ),
        applied_filters={
            "group_tokens": sorted(set(claims.group_tokens or ())),
            "usage_key": request.usage_key,
        },
        normalized_query=normalize_query(request.question),
        mode=str(request.mode),
    )


def read(key: str) -> dict | None:
    """The cached payload, or None. Never raises."""
    if not settings.response_cache_enabled:
        return None
    client = shared_state.get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        shared_state.redis_failed("response cache read", exc)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # Corrupt or hand-edited. Treat as a miss rather than failing the answer.
        return None
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
        return None
    return payload


def write(key: str, payload: dict, chunks) -> bool:
    """Store a payload. Returns whether it was stored. Never raises.

    Gated on `assert_cacheable`, which is the §6.4/§10.2 control: a response that
    retrieved anything from a per-student namespace is not stored and not served.
    It raises rather than skipping, and the raise is caught HERE and logged —
    loud in the log, invisible to the student, whose answer already streamed and
    must not fail because bookkeeping refused.
    """
    if not settings.response_cache_enabled:
        return False
    try:
        assert_cacheable(chunks)
    except PersonalDataNotCacheable:
        log.warning("response not cached: retrieval touched a personal namespace")
        return False

    client = shared_state.get_redis()
    if client is None:
        return False
    try:
        client.setex(key, settings.response_cache_ttl_seconds, json.dumps({**payload, "v": PAYLOAD_VERSION}))
        return True
    except Exception as exc:  # noqa: BLE001
        shared_state.redis_failed("response cache write", exc)
        return False
