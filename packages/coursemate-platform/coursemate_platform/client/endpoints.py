"""Typed calls to the service. Params and returns come from the shared contract,
so a field rename fails here rather than as a 422 in week three."""

from __future__ import annotations

import logging

from coursemate_contracts.ingest import (
    DeleteRequest,
    IngestAccepted,
    IngestRequest,
    LeafBlock,
)
from coursemate_contracts.invalidation import InvalidationNotice
from coursemate_contracts.metadata import ContentType

from . import http

log = logging.getLogger(__name__)

# The service routes everything under /coursemate. These calls go container to
# container and bypass Caddy, so they must carry the app's full path — Caddy is
# what makes the browser's /coursemate/... work, and it is not in this path.
_PREFIX = "/coursemate/api"
INGEST_BLOCKS = f"{_PREFIX}/ingest/blocks"
INGEST_DELETE = f"{_PREFIX}/ingest/delete"
INGEST_PRUNE = f"{_PREFIX}/ingest/prune"
INGEST_STATS = f"{_PREFIX}/ingest/stats"
INVALIDATE = f"{_PREFIX}/invalidate"

#: Open edX block type -> our content_type. Anything absent is not ingested.
_CONTENT_TYPE = {
    "html": ContentType.LESSON,
    "problem": ContentType.PROBLEM,
    "video": ContentType.TRANSCRIPT,
}


def send_leaves(*, tenant, course_id, offering_id, course_version, leaves, trace_id,
                run_id=None, is_final=False, topup=False) -> IngestAccepted:
    """One record per leaf block.

    That shape is deliberate: design §5.5 rule 1 says two blocks are never merged
    into one chunk, because a merged chunk could no longer cite a single
    usage_key. Sending leaves separately makes the service structurally incapable
    of merging across them.
    """
    request = IngestRequest(
        tenant=tenant,
        course_id=course_id,
        offering_id=offering_id,
        course_version=course_version,
        trace_id=trace_id,
        run_id=run_id or trace_id,
        is_final=is_final,
        topup=topup,
        blocks=[
            LeafBlock(
                usage_key=leaf.usage_key,
                block_id=leaf.block_id,
                block_type=leaf.block_type,
                version=leaf.version,
                display_name=leaf.display_name,
                text=leaf.text,
                content_type=_CONTENT_TYPE.get(leaf.block_type, ContentType.LESSON),
                group_tokens=list(leaf.group_tokens),
            )
            for leaf in leaves
        ],
    )
    return IngestAccepted(**http.post(INGEST_BLOCKS, request.model_dump(mode="json")))


def active_version(offering_id: str) -> str | None:
    """The index version currently being served, so a top-up lands inside it.

    **Any caller that activates blocks without replacing the course needs this.**
    A top-up writes its rows under the version the swap pointer already names —
    that is what `activate_usage_keys` matches on, and what lets a later reindex
    retire those rows along with everything else it replaces. Writing under any
    other version produces chunks nothing points at.

    Lived as a private helper in `tasks/reconcile.py` until the publish path
    needed the same value. Two copies of "which version are we serving" would be
    two things that must agree about the one field activation keys on, so it
    moved here, beside the other calls into the service.

    Returns None when the offering has never been indexed. Callers must treat
    that as "do not top up" rather than as a version: topping up an unindexed
    course writes rows under a pointer that does not exist.
    """
    try:
        return (http.get(f"{INGEST_STATS}/{offering_id}") or {}).get("active_version")
    except Exception:
        log.exception("could not read the active index version for %s", offering_id)
        return None


def prune_blocks(*, offering_id: str, usage_keys: list[str]) -> dict:
    """Drop every chunk for these blocks, active or not.

    Used before re-writing a block so a second copy cannot accumulate. Named
    `prune` on the service because it takes an explicit list rather than walking
    a subtree — a bug in tree-walking cannot cascade into removing more than was
    asked for.
    """
    if not usage_keys:
        return {"deleted_chunks": 0, "blocks": 0}
    return http.post(
        INGEST_PRUNE, {"offering_id": offering_id, "usage_keys": usage_keys}
    )


def send_delete(*, tenant, offering_id, usage_key, trace_id) -> dict:
    request = DeleteRequest(
        tenant=tenant, offering_id=offering_id, usage_key=usage_key, trace_id=trace_id
    )
    return http.post(INGEST_DELETE, request.model_dump(mode="json"))


def send_invalidation(*, tenant, reason, course_id, offering_id, student_id, trace_id) -> dict:
    notice = InvalidationNotice(
        tenant=tenant,
        reason=reason,
        course_id=course_id,
        offering_id=offering_id,
        student_id=student_id,
        trace_id=trace_id,
    )
    return http.post(INVALIDATE, notice.model_dump(mode="json"))
