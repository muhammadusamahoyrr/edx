"""What the sweep decides, with no Django in the way (§5.4).

It sits outside `tasks/` on purpose. That package's `__init__` imports every task
module so Celery can register them, which drags in Celery itself — so anything
living there is unimportable without a worker installed, and these tests would
need one to check set arithmetic.

The sweep itself needs a modulestore, a Celery worker and a running service. The
*decision* it makes needs neither, and separating them is not tidiness: the
dangerous part of a reconciliation sweep is not fetching the two lists, it is
what it concludes from them. That conclusion deletes content. It deserves tests
that run in under a second, without Open edX.

The one rule that matters here: **an empty or short read of the course tree must
never be read as "everything was unpublished."** `iter_course_leaves` yields
nothing when the course is missing on the published branch — which also happens
when the modulestore is briefly unavailable, when a course is mid-rerun, or when
someone passes a key that no longer resolves. Subtracting an empty `live` from a
healthy `indexed` marks every block an orphan, and the sweep would then delete
the entire index for that course while logging a successful run. The cap below
exists because that failure is silent, plausible, and unrecoverable without a
full re-bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Refuse to remove more than this fraction of a course in one sweep.
#: An instructor unpublishing over half a course in a day is rare; a transient
#: read failure presenting as exactly that is not. When the two are
#: indistinguishable from here, the recoverable mistake is to do nothing and say
#: so — a stale block is a correctness bug, a wiped index is an outage.
MAX_ORPHAN_FRACTION = 0.5


@dataclass(frozen=True)
class Drift:
    orphans: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Set when the orphan set was computed but refused. The blocks stay indexed
    #: and stay citable; the operator is told rather than the index being wiped.
    refused: str = ""

    @property
    def is_clean(self) -> bool:
        return not self.orphans and not self.missing and not self.refused


def compute_drift(
    live_keys: set[str],
    indexed_keys: set[str],
    *,
    max_orphan_fraction: float = MAX_ORPHAN_FRACTION,
    force: bool = False,
) -> Drift:
    """Compare the published tree against what is being served.

        orphans = indexed - live   -> unpublished or deleted, remove
        missing = live - indexed   -> a failed ingest or a gap, re-ingest

    `force` is the operator's override for the legitimate large-unpublish case,
    reachable only from the management command. The nightly task never sets it:
    an unattended job should not be able to empty an index.
    """
    orphans = sorted(indexed_keys - live_keys)
    missing = sorted(live_keys - indexed_keys)

    if not indexed_keys:
        # Nothing served yet. There is nothing to orphan, and `missing` is the
        # whole course — the caller decides whether that means "bootstrap me"
        # (it does) rather than treating it as drift.
        return Drift(orphans=[], missing=missing)

    if not force and orphans:
        if not live_keys:
            return Drift(
                orphans=[], missing=missing,
                refused=(
                    f"read 0 published blocks but {len(indexed_keys)} are indexed — "
                    f"treating this as a failed course read, not a mass unpublish; "
                    f"re-run with --force if the course really was emptied"
                ),
            )
        fraction = len(orphans) / len(indexed_keys)
        if fraction > max_orphan_fraction:
            return Drift(
                orphans=[], missing=missing,
                refused=(
                    f"{len(orphans)} of {len(indexed_keys)} indexed blocks "
                    f"({fraction:.0%}) are no longer published, above the "
                    f"{max_orphan_fraction:.0%} cap — refusing to remove them "
                    f"automatically; re-run with --force after checking the course"
                ),
            )

    return Drift(orphans=orphans, missing=missing)
