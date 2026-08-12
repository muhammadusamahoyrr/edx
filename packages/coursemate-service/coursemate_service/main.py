"""CourseMate service entrypoint.

Runs in its own container, outside Open edX (design §3.4). Everything expensive
lives here: retrieval, reranking, model calls. The LMS is not in the answer path.

Three credential classes are kept on separate routers on purpose (§3.4): a leaked
student token must not be able to write to the index.
"""

from __future__ import annotations

import logging

from coursemate_contracts import CONTRACT_VERSION
from fastapi import Depends, FastAPI, Response, status
from fastapi.responses import PlainTextResponse

from . import metrics, shared_state
from .api.chat import router as chat_router
from .api.deps import service_credential
from .api.examprep import router as examprep_router
from .api.ingest import router as ingest_router
from .api.invalidation import router as invalidation_router
from .api.packs import router as packs_router
from .config import settings
from .knowledge import get_store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Served behind the LMS origin at /coursemate/ (§3.4 v8), so the browser is
# same-origin and no gunicorn worker is held open for an answer.
#
# **No `root_path`, deliberately.** Caddy uses `handle` rather than `handle_path`,
# so it forwards the full path *including* the /coursemate prefix. Setting
# `root_path="/coursemate"` makes Starlette strip that prefix before matching, so
# a request for /coursemate/health tries to match "/health" and 404s against a
# route that plainly exists. The two settings must agree: either the proxy strips
# and the app declares bare paths, or neither does. We chose neither, because one
# fewer transformation is one fewer place for them to disagree.
app = FastAPI(title="CourseMate", version="0.1.0")

# Student-facing routes. Separate router from ingest/invalidation because they
# carry a different credential class (§3.4): a leaked student token must not be
# able to write to the index.
app.include_router(chat_router, prefix="/coursemate/api", tags=["student"])
# Feature B. Same credential class as chat — a student token, re-derived at the
# boundary — so it belongs on the student side of the §3.4 split.
app.include_router(examprep_router, prefix="/coursemate/api/examprep", tags=["student"])
# Service-credential routes. Kept on a separate router precisely so a leaked
# student token cannot reach them (§3.4).
app.include_router(ingest_router, prefix="/coursemate/api/ingest", tags=["ingest"])
app.include_router(invalidation_router, prefix="/coursemate/api/invalidate", tags=["ingest"])
# Loading a past-paper pack writes to the store, so it carries the service
# credential and never the student one — the same rule that keeps a leaked
# student token from writing to the chunk index.
app.include_router(packs_router, prefix="/coursemate/api/packs", tags=["ingest"])


@app.get("/coursemate/health")
def health() -> dict:
    """Liveness plus the contract version.

    The version is here so a mismatched platform package is detectable from the
    outside, rather than surfacing as a 422 on the first publish.
    """
    return {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "tenant": settings.tenant,
        "rerank_enabled": settings.rerank_enabled,
        "lexical_retrieval_enabled": settings.lexical_retrieval_enabled,
    }


@app.get("/coursemate/health/ready")
def ready(response: Response) -> dict:
    """Readiness: can this instance actually serve a student request?

    Distinct from liveness, and distinct in a way that matters: an **empty index
    is not a fault** — it is the `preparing` state (§5.1) — but an index that
    cannot be *opened* is, because every answer starts with a retrieval.

    Until 2026-08-13 this returned `{"status": "ready"}` unconditionally. A
    readiness probe that cannot fail is a liveness check with a misleading name:
    it would have reported ready throughout the D2 verification, while the
    service was up and unable to answer anything.

    What is checked, and what is deliberately not:

    * **The index store** — one cheap query that proves the file opens and the
      schema is there. Failing this means no question can be answered, so it is
      the one hard gate.
    * **Redis** — reported, never gating. `shared_state` documents unreachable
      Redis as a supported degraded mode: the limiter and authz cache fall back
      to per-process state. Refusing traffic because an optimisation is down
      would turn that documented degradation into an outage.
    * **The model provider** — not probed at all. A provider call per readiness
      check would cost a generation every few seconds, and provider failure is
      already a per-request state the student is told about (`unavailable`).
    * **The LMS** — not probed. The service legitimately starts before it, and
      enrollment is re-derived per request at the boundary anyway.

    Returns 503 when not ready, so an orchestrator can act on it rather than
    having to parse the body.
    """
    checks: dict[str, str] = {}

    try:
        get_store().indexed_offerings()
        checks["index"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.error("readiness: index store unavailable: %s", type(exc).__name__)
        checks["index"] = "unavailable"

    # Reported for operators, never gating — see above.
    try:
        client = shared_state.get_redis()
        checks["redis"] = "ok" if client is not None and client.ping() else "degraded"
    except Exception:  # noqa: BLE001
        checks["redis"] = "degraded"

    ready_now = checks["index"] == "ok"
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready_now else "not_ready", "checks": checks}


@app.get(
    "/coursemate/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(service_credential)],
    include_in_schema=False,
)
def prometheus_metrics() -> str:
    """Six counters, Prometheus text format.

    **Behind the service credential**, not open like `/health`. The numbers carry
    no student data — no identifiers, no question text — but `/coursemate/*` is
    reachable same-origin by any logged-in browser, and request volume is still a
    signal about an institution. A scraper sends the credential it already needs
    for ingest.

    `include_in_schema=False` because this is an operational surface, not part of
    the API anyone integrates against; publishing it in the spec would invite it
    to be treated as one.
    """
    return metrics.render()
