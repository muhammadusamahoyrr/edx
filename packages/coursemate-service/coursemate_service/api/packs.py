"""Loading a past-paper pack — service credential only (§3.4, §7.3).

**Not on the student router**, and the separation is the same one that keeps a
leaked student token from writing to the chunk index. A student may search a pack;
only the operator may load one.

**CLOs are confirmed by the person running the loader, not by the model** (§7.3,
§9.2 #2). Extraction is *assisted, never asserted*: a CLO list arriving with no
`confirmed_by` is loaded and served, but every tool result carries
`confirmed: false` so nothing presents it as the instructor's. The endpoint does
not silently stamp confirmation on the caller's behalf — that would turn "a human
confirmed this" into "a process ran", which is precisely the distinction §7.3
exists to preserve.
"""

from __future__ import annotations

import logging

from coursemate_contracts.examprep import ExamPrepPack
from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..knowledge import get_examprep_store
from .deps import contract_version_guard, service_credential

log = logging.getLogger(__name__)

# **The version lock belongs here too (added 2026-08-14).** `deps.py` described
# the guard as covering "the two server-to-server routers — ingest and
# invalidation". There are three: this one carries the same service credential
# and is called by the same operator tooling, and it was simply missed when the
# lock was wired. A pack load writes a term's worth of extracted papers that
# cannot be rebuilt from the modulestore, so of the three it is the one where a
# wire-format disagreement is least recoverable.
#
# Router-level, not per-route, so a NEW route here inherits both dependencies —
# which is the shape that would have prevented the omission in the first place.
router = APIRouter(
    dependencies=[Depends(service_credential), Depends(contract_version_guard)]
)


@router.post("/load")
def load_pack(pack: ExamPrepPack, force: bool = False) -> dict:
    """Replace this offering's questions and CLOs, atomically.

    Returns counts, never a bare `{"status": "ok"}`. A loader that reports success
    without saying what landed is how this project once indexed 226 blocks and
    served 26 — the caller has to be able to assert on a number.
    """
    if pack.tenant != settings.tenant:
        # Refused rather than rewritten. Loading another tenant's pack into this
        # deployment because the field disagreed would be a silent cross-tenant
        # write, and the whole point of carrying `tenant` from day one is that it
        # is checked.
        raise HTTPException(
            status_code=400,
            detail=f"pack tenant {pack.tenant!r} does not match this deployment",
        )
    if not pack.offering_id:
        raise HTTPException(status_code=400, detail="offering_id is required")

    # Duplicate question ids would violate the store's UNIQUE constraint mid-load
    # and roll the whole pack back. Caught here so the caller is told which id,
    # rather than reading an IntegrityError out of a 500.
    ids = [q.question_id for q in pack.questions]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise HTTPException(
            status_code=400, detail=f"duplicate question_id(s): {', '.join(dupes[:5])}"
        )

    mismatched = [q.question_id for q in pack.questions if q.offering_id != pack.offering_id]
    if mismatched:
        raise HTTPException(
            status_code=400,
            detail=f"question(s) scoped to another offering: {', '.join(mismatched[:5])}",
        )

    store = get_examprep_store()

    # Refuse a document already imported into this offering. A pack load
    # REPLACES the offering's questions, so a duplicate is not merely wasteful:
    # re-importing paper A after paper B silently discards B, and the counts
    # returned would look like a successful load.
    #
    # 409, not 400: the request is well-formed and the operator did nothing
    # wrong — the state is what conflicts. `force` exists because a corrected
    # extraction of the same PDF is a legitimate reload.
    previous = store.already_imported(
        tenant=pack.tenant, offering_id=pack.offering_id,
        content_sha256=pack.content_sha256,
    )
    if previous and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this document was already imported into {pack.offering_id} "
                f"({previous['questions']} questions, at {previous['loaded_at']}). "
                "Pass force=true to load it again."
            ),
        )

    counts = store.load_pack(pack)

    unconfirmed = sum(1 for c in pack.clos if not c.confirmed_by)
    if unconfirmed:
        log.warning(
            "pack for %s loaded with %d/%d unconfirmed CLOs; they will be served "
            "labelled unconfirmed (§7.3)",
            pack.offering_id, unconfirmed, len(pack.clos),
        )

    return {
        **counts,
        "offering_id": pack.offering_id,
        "unconfirmed_clos": unconfirmed,
        # Stated rather than implied: a pack with no hash cannot be checked for
        # duplication, and the operator should know that before loading again.
        "duplicate_checked": pack.content_sha256 is not None,
        "reloaded": bool(previous),
    }
