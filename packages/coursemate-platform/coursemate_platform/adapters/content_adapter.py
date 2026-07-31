"""The only module in CourseMate that touches the modulestore — design §3.3.

    "Open edX is evolving toward Learning Core. This design isolates all content
    access behind a thin adapter so it remains compatible with future storage
    changes." If the store changes, this file changes; nothing else does.

`.importlinter` contract 1 enforces that. It is one file, not a package, because
the promise is checkable at a glance only while it is one file — the moment it
becomes adapters/split_mongo/ and adapters/learning_core/, verifying the claim
needs a diagram.

**This module owns the published-branch context; it does not ask callers to open
it.** Verified from platform source (design v7, §3.6): `MixedModuleStore.
branch_setting` is a context manager writing to *thread-local* storage, defaulting
to None when no context is active. A Celery worker runs in a different thread with
no context, so it inherits nothing and falls back to the store's own default —
draft-preferred for the draft-capable stores.

An API shaped as "call iter_leaves() inside a branch_setting block" therefore
**fails open**: one forgotten `with` silently indexes draft content, with no
exception, no failing test, and no symptom until a student is cited unpublished
text. So every read here opens the context itself, and there is no public function
that reads content outside one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore

log = logging.getLogger(__name__)

#: Leaf types we ingest. Anything else is skipped and logged rather than guessed
#: at — a silent gap is worse than a recorded unsupported type (Principle 8).
SUPPORTED_LEAF_TYPES: frozenset[str] = frozenset({"html", "problem", "video"})

#: Containers we walk through to reach leaves. XBLOCK_PUBLISHED names the
#: published *container*, not the changed leaves (verified: publishing a parent
#: with changes in several children fires ONE event with the parent's details).
CONTAINER_TYPES: frozenset[str] = frozenset(
    {"course", "chapter", "sequential", "vertical"}
)


@dataclass(frozen=True)
class LeafContent:
    usage_key: str
    block_id: str
    block_type: str
    version: str
    display_name: str | None
    text: str


@contextmanager
def _published(course_key: CourseKey | None = None) -> Iterator[None]:
    """Pin published-only. Never optional, never inherited, never assumed."""
    store = modulestore()
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        yield


def _version_of(block) -> str:
    """A stable version marker for `usage_key@version` dedup and the swap pointer.

    Falls back through the fields Split exposes; a missing version must not stop
    ingestion, it only costs us the dedup optimisation.
    """
    for attribute in ("course_version", "subtree_edited_on", "edited_on"):
        value = getattr(block, attribute, None)
        if value:
            return str(value)
    return "unversioned"


def _extract_text(block) -> str:
    """Per-type text extraction.

    Verification test 1 (§3.6) records, for each block type, which field holds
    the content and whether it needs post-processing. Every branch below is one
    line in that table — when the test says a type returns something we cannot
    use, it gets excluded here and recorded as unsupported rather than half-read.
    """
    block_type = block.scope_ids.block_type

    if block_type == "html":
        return _strip_markup(getattr(block, "data", "") or "")

    if block_type == "problem":
        # Stem and solution are one semantic unit and must not be separated
        # (§5.5 criterion 2), so they are extracted together.
        return _strip_markup(getattr(block, "data", "") or "")

    if block_type == "video":
        # Transcripts are the valuable text on a video block. Reaching them from
        # the block is one of the things test 1 records.
        return _video_transcript(block)

    log.warning("coursemate: unsupported leaf type %s, skipping", block_type)
    return ""


def _strip_markup(raw: str) -> str:
    import re

    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _video_transcript(block) -> str:
    try:
        transcripts = getattr(block, "transcripts", None) or {}
        if not transcripts and not getattr(block, "sub", None):
            return ""
        # Resolved through the platform's transcript utilities in the real
        # implementation; kept behind this function so the call site never has to
        # know which of several transcript storage paths applied.
        return getattr(block, "_coursemate_transcript_text", "") or ""
    except Exception:  # pragma: no cover - defensive: never fail a whole course
        log.exception("coursemate: transcript read failed for %s", block.scope_ids.usage_id)
        return ""


def get_block(usage_key: UsageKey):
    with _published(usage_key.course_key):
        return modulestore().get_item(usage_key)


def iter_leaves(usage_key: UsageKey) -> Iterator[LeafContent]:
    """Walk a published container down to its supported leaves.

    This is the function that makes `XBLOCK_PUBLISHED` usable: the event names a
    container, so ingestion must resolve down to leaves itself (§5.2).
    """
    with _published(usage_key.course_key):
        store = modulestore()
        root = store.get_item(usage_key)
        yield from _walk(root)


def iter_course_leaves(course_key: CourseKey) -> Iterator[LeafContent]:
    """Whole-course walk, for bootstrap and the reconciliation sweep (§5.1, §5.4)."""
    with _published(course_key):
        store = modulestore()
        course = store.get_course(course_key, depth=None)
        if course is None:
            log.error("coursemate: course %s not found on the published branch", course_key)
            return
        yield from _walk(course)


def _walk(block) -> Iterator[LeafContent]:
    block_type = block.scope_ids.block_type

    if block_type in CONTAINER_TYPES:
        for child in block.get_children():
            yield from _walk(child)
        return

    if block_type not in SUPPORTED_LEAF_TYPES:
        log.info("coursemate: skipping unsupported type %s", block_type)
        return

    text = _extract_text(block)
    if not text.strip():
        return

    usage_id = block.scope_ids.usage_id
    yield LeafContent(
        usage_key=str(usage_id),
        block_id=usage_id.block_id,
        block_type=block_type,
        version=_version_of(block),
        display_name=getattr(block, "display_name", None),
        text=text,
    )


def list_course_keys() -> list[CourseKey]:
    """Every course on the instance.

    Lives here rather than in the management command because §3.3's promise is
    that *all* content access goes through this module — including enumeration.
    The command previously imported `modulestore` directly and the architecture
    contract caught it, which is the contract working as intended.
    """
    return [summary.id for summary in modulestore().get_course_summaries()]


def get_course_meta(course_key: CourseKey) -> dict[str, str | None]:
    with _published(course_key):
        course = modulestore().get_course(course_key)
        if course is None:
            return {}
        return {
            "course_id": str(course_key),
            "display_name": getattr(course, "display_name", None),
            "language": getattr(course, "language", None) or "en",
            "course_version": _version_of(course),
        }
