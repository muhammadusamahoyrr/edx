"""CourseMate service entrypoint.

Runs in its own container, outside Open edX (design §3.4). Everything expensive
lives here: retrieval, reranking, model calls. The LMS is not in the answer path.

Three credential classes are kept on separate routers on purpose (§3.4): a leaked
student token must not be able to write to the index.
"""

from __future__ import annotations

import logging

from coursemate_contracts import CONTRACT_VERSION
from fastapi import FastAPI

from .api.chat import router as chat_router
from .api.ingest import router as ingest_router
from .api.invalidation import router as invalidation_router
from .config import settings

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
# Service-credential routes. Kept on a separate router precisely so a leaked
# student token cannot reach them (§3.4).
app.include_router(ingest_router, prefix="/coursemate/api/ingest", tags=["ingest"])
app.include_router(invalidation_router, prefix="/coursemate/api/invalidate", tags=["ingest"])


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
def ready() -> dict:
    """Readiness. Distinct from liveness because an empty index is not a fault —
    it is the `preparing` state (§5.1), and the difference matters to a student."""
    return {"status": "ready"}
