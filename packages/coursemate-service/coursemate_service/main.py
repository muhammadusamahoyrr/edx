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

from .config import settings

log = logging.getLogger(__name__)

app = FastAPI(
    title="CourseMate",
    version="0.1.0",
    # Mounted behind the LMS origin at /coursemate/ (§3.4 v8), so the browser is
    # same-origin and no gunicorn worker is held for an answer.
    root_path="/coursemate",
)


@app.get("/health")
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


@app.get("/health/ready")
def ready() -> dict:
    """Readiness. Distinct from liveness because an empty index is not a fault —
    it is the `preparing` state (§5.1), and the difference matters to a student."""
    return {"status": "ready"}
