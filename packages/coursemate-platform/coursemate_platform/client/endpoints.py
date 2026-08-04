"""Typed calls to the service. Params and returns come from the shared contract,
so a field rename fails here rather than as a 422 in week three."""

from __future__ import annotations

from coursemate_contracts.ingest import (
    DeleteRequest,
    IngestAccepted,
    IngestRequest,
    LeafBlock,
)
from coursemate_contracts.invalidation import InvalidationNotice
from coursemate_contracts.metadata import ContentType

from . import http

# The service routes everything under /coursemate. These calls go container to
# container and bypass Caddy, so they must carry the app's full path — Caddy is
# what makes the browser's /coursemate/... work, and it is not in this path.
_PREFIX = "/coursemate/api"
INGEST_BLOCKS = f"{_PREFIX}/ingest/blocks"
INGEST_DELETE = f"{_PREFIX}/ingest/delete"
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
