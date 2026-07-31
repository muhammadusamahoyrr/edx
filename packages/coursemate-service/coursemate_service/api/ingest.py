"""Ingest API — platform worker to service (design §3.4 hop 3, §5.3).

Carries the **service credential**, never a student token: a leaked student-path
token must not be able to write to the index.

The platform sends one record per leaf block; this endpoint chunks, stores,
verifies and swaps. That split is the one resolved in the structure plan — the
platform *reads* (modulestore is a Python API, unreachable over the network), the
service *transforms*. It keeps the chunker, its cache and the swap logic together
in one process where write→verify→swap is a local transaction rather than a
distributed protocol.
"""

from __future__ import annotations

import logging
from coursemate_contracts.ingest import DeleteRequest, IngestAccepted, IngestRequest
from fastapi import APIRouter, Depends

from ..ingestion.chunking import chunk_block
from ..knowledge import get_store
from .deps import service_credential

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(service_credential)])


@router.post("/blocks", response_model=IngestAccepted)
async def ingest_blocks(request: IngestRequest) -> IngestAccepted:
    """Chunk and index a batch of published leaf blocks.

    §5.5 rule 1 — two blocks are never merged into one chunk — is enforced by the
    wire format rather than by a conditional here: each leaf arrives as its own
    record, so the chunker only ever sees one block's text and is structurally
    incapable of merging across them.
    """
    # The RUN is the version, so every batch of one reindex lands under the
    # same version and the active pointer flips exactly once (§5.3).
    version = request.run_id
    store = get_store()

    rows: list[dict] = []
    failed: list[str] = []

    for block in request.blocks:
        try:
            for chunk in chunk_block(block.text):
                rows.append(
                    {
                        "tenant": request.tenant,
                        "course_id": request.course_id,
                        "offering_id": request.offering_id,
                        "usage_key": block.usage_key,
                        "block_id": block.block_id,
                        "block_type": block.block_type,
                        "content_type": block.content_type.value,
                        "display_name": block.display_name,
                        "version": version,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                    }
                )
        except Exception:  # noqa: BLE001
            # A block that fails must be *detectable*, never a silent gap
            # (Principle 8) — it is returned so the worker records it.
            log.exception("chunking failed for %s", block.usage_key)
            failed.append(block.usage_key)

    written = store.write_chunks(rows)

    # Swap only when the run says it is complete. Mid-run the new chunks sit
    # INACTIVE, so the previously indexed course keeps serving until the whole
    # run has landed — which is the entire point of write-then-swap: a failure
    # halfway through leaves the last good index intact rather than a hole.
    ok = True
    if request.topup:
        # Sweep repair (§5.4): activate just these blocks. No swap — see
        # activate_usage_keys for why re-swapping here would be a race.
        store.activate_usage_keys(
            request.offering_id, version, [b.usage_key for b in request.blocks]
        )
    elif request.is_final:
        ok = store.verify_run(request.offering_id, version)
        if ok:
            store.swap(request.offering_id, version)
        else:
            log.error("verify failed; NOT swapping %s — previous index left intact",
                      request.offering_id)

    log.info(
        "ingest %s: %d blocks -> %d chunks, swapped=%s",
        request.offering_id, len(request.blocks), written, ok,
    )
    return IngestAccepted(
        trace_id=request.trace_id,
        blocks_received=len(request.blocks),
        blocks_written=len(request.blocks) - len(failed),
        chunks_written=written,
        failed=failed,
        swapped=ok,
    )


@router.post("/delete")
async def delete_blocks(request: DeleteRequest) -> dict:
    """XBLOCK_DELETED, or an orphan found by the reconciliation sweep (§5.4)."""
    store = get_store()
    with store._lock:  # noqa: SLF001 - single-writer store, deliberate
        ids = [
            r["id"] for r in store._conn.execute(  # noqa: SLF001
                "SELECT id FROM chunks WHERE offering_id=? AND usage_key LIKE ?",
                (request.offering_id, request.usage_key + "%"),
            )
        ]
        if ids:
            marks = ",".join("?" * len(ids))
            store._conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)  # noqa: SLF001
            store._conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", ids)  # noqa: SLF001
            store._conn.commit()  # noqa: SLF001
    return {"deleted_chunks": len(ids)}


@router.get("/manifest/{offering_id:path}")
async def manifest(offering_id: str) -> dict:
    """Every block currently served for this offering.

    The reconciliation sweep (§5.4) needs this because **no unpublish event
    exists in openedx-events.** Nothing tells us when an instructor unpublishes a
    unit, so the only way to detect it is to periodically compare what we serve
    against what the platform actually publishes. Without this the tutor keeps
    answering from — and citing — content students can no longer see, which is a
    direct Principle 3 violation that no event can catch.
    """
    keys = get_store().indexed_usage_keys(offering_id)
    return {"offering_id": offering_id, "usage_keys": keys, "count": len(keys)}


@router.post("/prune")
async def prune(request: dict) -> dict:
    """Remove blocks the sweep found to be orphaned.

    Separate from /delete, which drops a subtree on XBLOCK_DELETED. This takes an
    explicit list the sweep computed, so a bug in tree-walking cannot cascade
    into removing more than was asked for.
    """
    offering_id = str(request.get("offering_id") or "")
    usage_keys = [str(k) for k in (request.get("usage_keys") or [])]
    if not offering_id or not usage_keys:
        return {"deleted_chunks": 0, "blocks": 0}
    removed = get_store().delete_usage_keys(offering_id, usage_keys)
    return {"deleted_chunks": removed, "blocks": len(usage_keys)}


@router.get("/offerings")
async def offerings() -> dict:
    """Courses the nightly sweep should visit.

    Declared before /stats/{offering_id:path} would otherwise be reachable — a
    path-converter route is greedy, so ordering here is load-bearing.
    """
    ids = get_store().indexed_offerings()
    return {"offerings": ids, "count": len(ids)}


@router.get("/stats/{offering_id:path}")
async def stats(offering_id: str) -> dict:
    return get_store().stats(offering_id)
