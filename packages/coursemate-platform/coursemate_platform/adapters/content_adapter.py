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

#: The tutor block's category. Its presence in a course is the opt-in signal for
#: indexing — see `course_has_tutor`. Matches the `xblock.v1` entry point name.
TUTOR_BLOCK_TYPE = "coursemate_tutor"


@dataclass(frozen=True)
class LeafContent:
    usage_key: str
    block_id: str
    block_type: str
    version: str
    display_name: str | None
    text: str
    #: Access groups this block is restricted to, as `"<partition>:<group>"`
    #: tokens. Empty means every enrolled student may see it.
    #:
    #: **Why these travel with the chunk instead of being filtered here.**
    #: `visible_to_staff_only` is absolute — no student may ever see it, so it is
    #: dropped in `_walk` and never reaches the index. Group restrictions are
    #: *conditional*: a verified-track student SHOULD receive verified-only
    #: content, and a cohort member SHOULD receive their cohort's content.
    #: Dropping those at index time would protect audit students by breaking the
    #: product for the students who paid. So the restriction is carried and
    #: resolved per caller at query time, in the SQL, alongside the tenant and
    #: offering filters (§6.3).
    group_tokens: tuple[str, ...] = ()


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


def _group_tokens(block) -> tuple[str, ...]:
    """Access restrictions on one block, as `"<partition>:<group>"` tokens.

    `group_access` is `{partition_id: [group_id, ...]}` and is how Open edX
    expresses every conditional visibility rule we care about — cohort content
    groups and enrollment-track gating both land here. Partition ids are
    instance configuration, so they are recorded verbatim rather than mapped
    against constants we would then have to keep in step with each release.

    An empty result means unrestricted, which is the common case.
    """
    raw = getattr(block, "group_access", None) or {}
    try:
        return tuple(
            f"{partition_id}:{group_id}"
            for partition_id, group_ids in raw.items()
            for group_id in (group_ids or [])
        )
    except AttributeError:  # pragma: no cover - defensive: never fail a course
        log.warning("coursemate: unreadable group_access on %s", block.scope_ids.usage_id)
        return ()


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


#: Where `get_transcript` lives, newest location first. **It moved between
#: releases** — `xmodule.video_block.transcripts_utils` up to and including
#: Sumac, `openedx.core.djangoapps.video_config.transcripts_utils` after it
#: (verified against both branches of openedx/edx-platform). Importing one path
#: would raise ImportError on the other, and since this module is imported at
#: Celery startup that is not a degraded feature, it is a dead worker.
#:
#: Order matters only for correctness of preference, not behaviour: the two are
#: the same function, and on any given deployment exactly one resolves.
_TRANSCRIPT_MODULES = (
    "openedx.core.djangoapps.video_config.transcripts_utils",
    "xmodule.video_block.transcripts_utils",
)


def _transcript_resolver():
    """The platform's own `get_transcript`, or None if this release has neither.

    Resolved once and cached on the function, because it is stable for the life
    of the process and this runs per video block.
    """
    cached = getattr(_transcript_resolver, "_fn", "unset")
    if cached != "unset":
        return cached

    import importlib

    resolved = None
    for name in _TRANSCRIPT_MODULES:
        try:
            resolved = getattr(importlib.import_module(name), "get_transcript", None)
        except ImportError:
            continue
        if resolved is not None:
            log.info("coursemate: transcript resolver from %s", name)
            break

    _transcript_resolver._fn = resolved  # noqa: SLF001
    return resolved


def _video_transcript(block) -> str:
    """Transcript text for a video block, via the platform's own resolver.

    **Why not read a store directly.** Transcripts live in TWO places depending
    on how the video was uploaded: `edx-val` when the block carries an
    `edx_video_id`, the contentstore (as a `subs_<id>.srt.sjson` asset)
    otherwise. Picking one here would work on a course that uses that path and
    return nothing on a course that uses the other — and that failure is
    indistinguishable from "this video genuinely has no transcript", which is
    exactly the silent gap Principle 8 forbids.

    `transcripts_utils.get_transcript` is the platform's own resolver and tries
    both. Inheriting platform behaviour rather than reimplementing it is the same
    rule §10.1 applies to authorization.
    """
    get_transcript = _transcript_resolver()
    if get_transcript is None:
        log.warning(
            "coursemate: no transcript resolver on this platform; video %s not ingested",
            block.scope_ids.usage_id,
        )
        return ""

    try:
        # Positional block, then keyword: the signature is
        # get_transcript(video, lang=None, output_format=Transcript.SRT, ...)
        # and SRT would carry sequence numbers and timestamps into the index.
        # "txt" is the literal value of Transcript.TXT.
        text, _filename, _mimetype = get_transcript(block, output_format="txt")
    except OSError as exc:
        # A transcript RECORD exists but its file is missing from storage.
        # Observed on DemoX: edx-val rows point at
        # /openedx/media/video-transcripts/<hash>.srt files the course import
        # never shipped. Distinct from "no transcript authored" and worth a
        # WARNING, because it is an operator-fixable storage problem rather than
        # an authoring choice — and it is invisible otherwise.
        #
        # Deliberately NOT falling back to the contentstore here. `get_transcript`
        # falls back only on NotFoundError, so an OSError skips its own fallback;
        # reaching around that would make the tutor cite transcripts the
        # platform's own video player cannot display. Matching platform
        # behaviour beats being quietly more capable than it.
        log.warning(
            "coursemate: transcript file missing for %s (%s)",
            block.scope_ids.usage_id, exc,
        )
        return ""
    except Exception:  # noqa: BLE001 - NotFoundError and friends; never fail a course
        # INFO not WARNING: a video with no transcript is normal authoring, not a
        # fault. The loud case is a *supported* block yielding nothing, and
        # `_walk` logs that once for every type rather than only for video.
        log.info("coursemate: no transcript for %s", block.scope_ids.usage_id)
        return ""

    return " ".join((text or "").split())


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

    usage_id = block.scope_ids.usage_id

    # Staff-only is ABSOLUTE and therefore filtered here, at index time: no
    # student may ever see it, so there is no caller for whom it would be a
    # correct result and no reason to carry it. Publishing does NOT clear this
    # flag — a block can be published and staff-only at once — so the published
    # branch pin does not cover it.
    if getattr(block, "visible_to_staff_only", False):
        log.info("coursemate: staff-only block %s not indexed", usage_id)
        return

    text = _extract_text(block)
    if not text.strip():
        # A SUPPORTED type that yields nothing is a gap, not a non-event. Without
        # this line every video block vanished with no trace, because `video` is
        # on SUPPORTED_LEAF_TYPES and so never hit the unsupported-type branch
        # above — a silent absence of exactly the kind Principle 8 forbids.
        log.warning(
            "coursemate: %s block %s yielded no text, skipping", block_type, usage_id
        )
        return

    yield LeafContent(
        usage_key=str(usage_id),
        block_id=usage_id.block_id,
        block_type=block_type,
        version=_version_of(block),
        display_name=getattr(block, "display_name", None),
        text=text,
        group_tokens=_group_tokens(block),
    )


def list_course_keys() -> list[CourseKey]:
    """Every course on the instance.

    Lives here rather than in the management command because §3.3's promise is
    that *all* content access goes through this module — including enumeration.
    The command previously imported `modulestore` directly and the architecture
    contract caught it, which is the contract working as intended.
    """
    return [summary.id for summary in modulestore().get_course_summaries()]


def user_group_tokens(course_key: CourseKey, user) -> tuple[str, ...]:
    """Which access groups a user belongs to, in the same token form as blocks.

    Read through the platform's own partition helpers so enrollment-track and
    content-group ids come from the instance's configuration. Deriving them
    service-side would mean hard-coding partition and group ids that are
    configurable per deployment — correct on our stack, quietly wrong on someone
    else's, and wrong in the direction that shows a paying student less than they
    paid for.

    **Deliberately NOT `PartitionService.get_user_group_id_for_partition`.** Its
    docstring is explicit: if the user has no group yet it *assigns one and
    persists that decision*. That is a write, and this runs on every token mint.
    On a course using split-test experiments it would have enrolled students into
    A/B groups merely for opening the tutor, quietly corrupting the instructor's
    experiment — a side effect with no error and no symptom until someone read
    the results. `get_user_partition_groups` resolves without assigning.

    Returns empty on any failure, which denies restricted content rather than
    granting it. That is the same fail-closed choice §10.1 makes for enrollment:
    a student briefly missing gated content is recoverable, an audit student
    reading verified-only material is not.
    """
    try:
        from xmodule.partitions.partitions_service import (
            get_all_partitions_for_course,
            get_user_partition_groups,
        )
    except ImportError:  # pragma: no cover - platform-only import
        log.warning("coursemate: partitions_service unavailable; no group access granted")
        return ()

    try:
        with _published(course_key):
            course = modulestore().get_course(course_key)
        if course is None:
            return ()

        # `active_only=True` and keying on 'id' are not arbitrary: this is
        # exactly what `lms.djangoapps.course_blocks.transformers.user_partitions
        # .UserPartitionTransformer` does to decide what a student may see. Using
        # the same two calls is what makes our filter AGREE with courseware
        # rather than approximate it.
        partitions = get_all_partitions_for_course(course, active_only=True)
        groups = get_user_partition_groups(course_key, partitions, user, "id")
        return tuple(f"{pid}:{group.id}" for pid, group in groups.items())
    except Exception:  # noqa: BLE001 - never fail a mint over an access lookup
        log.exception("coursemate: partition lookup failed for %s", course_key)
        return ()


def course_has_tutor(course_key: CourseKey) -> bool:
    """Has anyone opted this course in by adding the tutor block?

    The consent signal for indexing. A course whose staff never added the block
    has not asked for CourseMate, and reading its content anyway is not a
    technical error but a trust one — `tasks/reconcile.py` already refuses to
    sweep courses nobody opted into for exactly this reason, and `--all` used to
    disagree with it.

    Enumeration lives here rather than in the command because §3.3's promise is
    that *all* content access goes through this module.
    """
    with _published(course_key):
        return bool(
            modulestore().get_items(course_key, qualifiers={"category": TUTOR_BLOCK_TYPE})
        )


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
