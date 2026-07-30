"""The ingest hop — platform worker to service.

Design §0.2 of the structure plan resolved where the work happens: **the platform
worker reads, the service transforms.** The worker resolves a published container
down to leaves and extracts text; the service chunks, embeds, writes, verifies and
swaps. Embedding cache and embedder therefore sit together (§6.4), and §5.3's
four-step swap stays one local transaction instead of a distributed protocol.

**One record per leaf block, and that is load-bearing.** §5.5 rule 1 says two
blocks are never merged into one chunk, because a merged chunk could no longer
cite a single usage_key. Here that invariant lives in the wire format rather than
in a chunker's conditional: the service receives leaves separately and is
structurally incapable of merging across them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .metadata import ContentType


class LeafBlock(BaseModel):
    """One published leaf block, extracted in-platform."""

    usage_key: str
    block_id: str
    block_type: str
    version: str

    display_name: str | None = None
    #: Plain text. Whatever per-type extraction was needed (§3.6 test 1) already
    #: happened platform-side, because interpreting a block may need platform code.
    text: str
    content_type: ContentType

    week: int | None = Field(default=None, ge=0)
    publish_time: datetime | None = None


class IngestRequest(BaseModel):
    """A batch of leaves for one course, from one bootstrap run or one publish."""

    tenant: str
    course_id: str
    offering_id: str
    course_version: str | None = None

    blocks: list[LeafBlock]

    #: Correlates a batch with the Celery task that produced it, so a partial
    #: ingest is traceable to its trigger (§11.4).
    trace_id: str


class IngestAccepted(BaseModel):
    """What the service reports back after write-verify-swap (§5.3)."""

    trace_id: str
    blocks_received: int
    blocks_written: int
    chunks_written: int
    #: Leaves that failed permanently. The worker records these in
    #: `failed_ingestions` so the reconciliation sweep can report them (§5.2).
    #: A gap must be detectable, never silent (Principle 8).
    failed: list[str] = Field(default_factory=list)
    swapped: bool


class DeleteRequest(BaseModel):
    """XBLOCK_DELETED, or an orphan found by the reconciliation sweep (§5.4)."""

    tenant: str
    offering_id: str
    #: Every chunk under this subtree goes.
    usage_key: str
    trace_id: str


class ReconcileReport(BaseModel):
    """What the nightly sweep found (§5.4).

    The sweep is the only mitigation for unpublish, since no platform event
    exists — so its output is a live guarantee's evidence, not a log line.
    """

    offering_id: str
    live_blocks: int
    indexed_blocks: int
    orphans_removed: list[str] = Field(default_factory=list)
    missing_enqueued: list[str] = Field(default_factory=list)
    retried_failures: list[str] = Field(default_factory=list)
