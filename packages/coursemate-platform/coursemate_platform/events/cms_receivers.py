"""Studio-side receivers — design §5.2.

**Every function here does one thing: validate and enqueue.**

`openedx-events` signals are Django signals, so a receiver runs *synchronously, in
the request that published the content* — and `XBLOCK_PUBLISHED` is a
`content_authoring` event, so that request is the instructor clicking **Publish**
in Studio. Running extract -> chunk -> embed inline would hang that button on
third-party network I/O: publishing a section with 40 leaves would mean 40
embedding round-trips inside one request. If the embedding provider is slow,
Publish is slow; if it is down, **Publish fails.** That makes a core platform
action depend on our vendor's uptime, which Principle 8 forbids outright.

This mirrors the platform's own pattern — `content/search` pairs signal handlers
with Celery tasks for exactly this reason.

**Deliberately not subscribed: `XBLOCK_CREATED` and `XBLOCK_UPDATED`.** They exist,
they fire constantly, and their absence here is a decision rather than an
oversight. They fire on *draft* edits. Subscribing would index unpublished content
and violate Principle 3 silently — no error, no failing test, no symptom until a
student is cited something they cannot see. The platform's own search app *does*
subscribe to them, because Studio search deliberately indexes drafts; we are
published-only, so we diverge on purpose.
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger(__name__)


def on_xblock_published(signal, sender, xblock_info, **kwargs):  # noqa: ARG001
    """XBLOCK_PUBLISHED. Enqueue and return.

    The payload names the published **container**, not the changed leaves —
    publishing a parent with changes in several children fires ONE event with the
    parent's details. Resolving down to leaves is the worker's job (§5.2).
    """
    from ..tasks.ingest import ingest_published_block

    usage_key = str(xblock_info.usage_key)
    version = str(getattr(xblock_info, "version", "") or "")

    ingest_published_block.delay(
        usage_key=usage_key, version=version, trace_id=str(uuid.uuid4())
    )

    # Also sweep this course (§5.4). Publishing is the one moment we KNOW the
    # course tree changed, and unpublishing a unit is frequently followed by
    # publishing something else — so running the sweep here shortens the window
    # during which unpublished content can still be cited, from "up to a night"
    # to "until the next publish". It does not close it: without an unpublish
    # event, nothing can.
    from ..tasks.reconcile import reconcile_course

    reconcile_course.delay(str(xblock_info.usage_key.course_key))
    log.info("coursemate: enqueued ingest + sweep for %s", usage_key)


def on_xblock_deleted(signal, sender, xblock_info, **kwargs):  # noqa: ARG001
    """XBLOCK_DELETED. Drop every chunk under that subtree."""
    from ..tasks.ingest import delete_block

    delete_block.delay(
        usage_key=str(xblock_info.usage_key), trace_id=str(uuid.uuid4())
    )


def on_xblock_duplicated(signal, sender, xblock_info, **kwargs):  # noqa: ARG001
    """XBLOCK_DUPLICATED. The copy is a new usage_key and needs its own chunks."""
    from ..tasks.ingest import ingest_published_block

    target = getattr(xblock_info, "usage_key", None)
    if target is None:
        return
    ingest_published_block.delay(
        usage_key=str(target), version="", trace_id=str(uuid.uuid4())
    )


# Designed, not built in the delivery window (§5.4): COURSE_IMPORT_COMPLETED and
# COURSE_RERUN_COMPLETED. The platform's own search app subscribes to both and
# runs ONE bulk task rather than thousands of per-block events, which is the shape
# to copy when these land. Until then an imported course is indexed when someone
# runs the bootstrap command or clicks the Studio button — the index is not
# wrong, it is *absent*, and the query-time backstop says so rather than claiming
# the content does not exist.
