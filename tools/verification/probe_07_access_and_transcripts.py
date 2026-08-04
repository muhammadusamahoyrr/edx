"""Probe 7 — video transcripts and block-level access, against the live stack.

    tutor local run cms python /openedx/probes/probe_07_access_and_transcripts.py

Three things landed in `content_adapter` written from edx-platform *source* but
never executed here, and each fails in a direction that produces no error:

  A. the transcript resolver moved modules between releases — the wrong import
     is a dead Celery worker, not a missing feature
  B. `_video_transcript` returns "" for a video with no transcript AND for a
     resolver that never loaded; those look identical from the index
  C. `user_group_tokens` returns () both when a user has no groups and when the
     lookup failed — the fail-closed default hides its own failure

So every check below prints what it actually read. **This probe writes nothing.**
That is deliberate and load-bearing: the first draft of `user_group_tokens`
called `PartitionService.get_user_group_id_for_partition`, which *assigns and
persists* a group when the user has none. A probe that reproduced that call
would have created the very experiment-group assignments it was meant to
detect.

It exercises the shipped functions rather than reimplementing them. A probe that
re-derives the logic tests the probe.
"""

from __future__ import annotations

import os
import sys

import django

django.setup()

sys.path.insert(0, "/openedx/probes")

from probe_report import Confidence, Finding, environment_block  # noqa: E402

#: Overridable, because the interesting course is often not the demo one — a
#: course with cohorts or a verified track is what actually exercises section C.
COURSE = os.environ.get(
    "COURSEMATE_PROBE_COURSE", "course-v1:OpenedX+DemoX+DemoCourse"
)


def main() -> None:
    from opaque_keys.edx.keys import CourseKey
    from xmodule.modulestore import ModuleStoreEnum
    from xmodule.modulestore.django import modulestore

    from coursemate_platform.adapters import content_adapter

    course_key = CourseKey.from_string(COURSE)
    store = modulestore()

    f = Finding(
        probe_id="Probe 7",
        title="Video transcripts and block-level access",
        objective=(
            "Determine, on this instance: which transcript resolver exists, whether a "
            "video block yields usable text, whether any block carries a visibility or "
            "group restriction, and whether the partition lookup returns tokens that "
            "match what blocks are keyed on."
        ),
        method=(
            "Call the shipped `content_adapter` functions inside the CMS, against the "
            "published branch, on a real course. Read-only throughout."
        ),
    )
    f.command(f"tutor local run cms python /openedx/probes/probe_07_access_and_transcripts.py")
    f.source("packages/coursemate-platform/coursemate_platform/adapters/content_adapter.py")
    f.source("https://github.com/openedx/edx-platform/blob/master/xmodule/partitions/partitions_service.py")

    # --- A. which transcript resolver is on this release ---------------------
    resolver = content_adapter._transcript_resolver()  # noqa: SLF001
    f.evidence("transcript resolver found", bool(resolver))
    if resolver is not None:
        f.evidence("resolver module", getattr(resolver, "__module__", "?"))
        f.conclude(
            Confidence.CONFIRMED,
            f"`get_transcript` resolves from `{getattr(resolver, '__module__', '?')}` "
            f"on this release.",
        )
    else:
        f.conclude(
            Confidence.CONFIRMED,
            "NEITHER transcript module exists on this release. Every video block will "
            "be skipped and logged. _TRANSCRIPT_MODULES needs this release's path.",
        )

    # --- B. video blocks: pointer present, and does text come back? ----------
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        videos = store.get_items(course_key, qualifiers={"category": "video"})
        f.evidence("published video blocks in course", len(videos))

        with_pointer = 0
        with_text = 0
        for block in videos:
            sub = getattr(block, "sub", None)
            transcripts = getattr(block, "transcripts", None) or {}
            edx_video_id = getattr(block, "edx_video_id", None)
            if sub or transcripts or edx_video_id:
                with_pointer += 1

            text = content_adapter._video_transcript(block)  # noqa: SLF001
            if text.strip():
                with_text += 1
                if with_text == 1:  # record one real sample, not all of them
                    f.evidence("first video usage_key", block.scope_ids.usage_id)
                    f.evidence("  .sub", sub)
                    f.evidence("  .transcripts", transcripts)
                    f.evidence("  .edx_video_id", edx_video_id)
                    f.evidence("  extracted chars", len(text))
                    f.evidence("  extracted sample", text[:200])

        f.evidence("videos carrying a transcript pointer", with_pointer)
        f.evidence("videos yielding extractable text", with_text)

    if not videos:
        f.conclude(Confidence.UNVERIFIED,
                   "No published video in this course — transcript extraction is UNTESTED. "
                   "Add one video with an .srt in Studio and re-run.")
    elif with_text:
        f.conclude(Confidence.CONFIRMED,
                   f"{with_text} of {len(videos)} video block(s) yield transcript text. "
                   "Video content will reach the index.")
    elif with_pointer:
        f.conclude(Confidence.CONFIRMED,
                   f"{with_pointer} video(s) carry a transcript pointer but NONE yielded "
                   "text. The resolver is reachable and still returning nothing — this is "
                   "the real failure, not an authoring gap.")
    else:
        f.conclude(Confidence.CONFIRMED,
                   "No video in this course has a transcript at all. Nothing to extract; "
                   "the code path remains untested rather than broken.")

    # --- C. does anything in this course carry a restriction? ----------------
    staff_only: list[str] = []
    restricted: list[tuple[str, str]] = []
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        for category in ("html", "problem", "video"):
            for block in store.get_items(course_key, qualifiers={"category": category}):
                if getattr(block, "visible_to_staff_only", False):
                    staff_only.append(str(block.scope_ids.usage_id))
                tokens = content_adapter._group_tokens(block)  # noqa: SLF001
                if tokens:
                    restricted.append((str(block.scope_ids.usage_id), ",".join(tokens)))

    f.evidence("staff-only leaf blocks", len(staff_only))
    f.evidence("group-restricted leaf blocks", len(restricted))
    for usage_key, tokens in restricted[:5]:
        f.evidence(f"  restricted {usage_key}", tokens)

    if restricted:
        f.conclude(Confidence.CONFIRMED,
                   f"{len(restricted)} block(s) carry group_access. The query-time filter "
                   "has real data to be tested against.")
    else:
        f.conclude(Confidence.CONFIRMED,
                   "NO block in this course carries group_access. The filter is unit-tested "
                   "only. Create a cohort- or track-restricted unit to test it live.")
        f.limitation("Absence here does not mean enrollment-track gating is absent from the "
                     "platform — it may be applied at render time instead of stored on the "
                     "block. Section D is what distinguishes those two cases.")

    # --- D. the caller side: does the partition lookup return anything? ------
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(is_superuser=True).first()
    f.evidence("probe user", getattr(user, "username", None))
    if user is None:
        f.conclude(Confidence.UNVERIFIED, "No superuser found; partition lookup not exercised.")
    else:
        tokens = content_adapter.user_group_tokens(course_key, user)
        f.evidence("user_group_tokens(admin)", tokens or "() — empty")

        # Distinguish "no groups" from "lookup failed": read the partitions
        # directly. Empty tokens with partitions present means the lookup is
        # broken; empty tokens with no partitions means the course has none.
        try:
            from xmodule.partitions.partitions_service import get_all_partitions_for_course

            with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
                course = store.get_course(course_key)
            partitions = get_all_partitions_for_course(course, active_only=True)
            f.evidence("active partitions on course", len(partitions))
            for p in partitions[:6]:
                f.evidence(f"  partition {p.id}", f"{p.name} scheme={p.scheme.name}")
        except Exception as exc:  # noqa: BLE001
            partitions = []
            f.evidence("partition enumeration failed", f"{type(exc).__name__}: {exc}")

        if tokens:
            f.conclude(Confidence.CONFIRMED,
                       f"Partition lookup works: {len(tokens)} token(s) for this user.")
        elif partitions:
            f.conclude(Confidence.CONFIRMED,
                       f"{len(partitions)} active partition(s) exist but the lookup returned "
                       "NOTHING. Either this user is in no group, or user_group_tokens is "
                       "failing and swallowing it. Check the CMS log for "
                       "'partition lookup failed'.")
        else:
            f.conclude(Confidence.CONFIRMED,
                       "No active partitions on this course, so empty tokens are correct. "
                       "The lookup is still UNTESTED against a course that has partitions.")

    # --- E. opt-in: does the tutor block make this course eligible? ----------
    has_tutor = content_adapter.course_has_tutor(course_key)
    f.evidence("course_has_tutor()", has_tutor)
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        tutor_blocks = store.get_items(
            course_key, qualifiers={"category": content_adapter.TUTOR_BLOCK_TYPE}
        )
    f.evidence("tutor blocks found", len(tutor_blocks))
    f.conclude(
        Confidence.CONFIRMED,
        f"Opt-in check returns {has_tutor} for a course containing {len(tutor_blocks)} "
        f"tutor block(s). `--all` will {'include' if has_tutor else 'SKIP'} this course.",
    )

    # --- F. what the walk actually produces now ------------------------------
    leaves = list(content_adapter.iter_course_leaves(course_key))
    by_type: dict[str, int] = {}
    carrying_tokens = 0
    for leaf in leaves:
        by_type[leaf.block_type] = by_type.get(leaf.block_type, 0) + 1
        if leaf.group_tokens:
            carrying_tokens += 1
    f.evidence("leaves yielded", len(leaves))
    f.evidence("leaves by type", by_type)
    f.evidence("leaves carrying group tokens", carrying_tokens)
    f.conclude(
        Confidence.CONFIRMED,
        f"iter_course_leaves yields {by_type}. A 'video' entry here is the end-to-end "
        f"proof that transcripts reach ingestion; its absence means they do not.",
    )

    f.implies("If section A found no resolver, add this release's module path to "
              "_TRANSCRIPT_MODULES before anything else — every video is silently lost.")
    f.implies("If section D shows partitions but no tokens, the block-level access filter "
              "is hiding restricted content from EVERY caller, including those entitled "
              "to it. Fails closed, so it leaks nothing, but it is not working.")
    f.limitation("One course, one user, one release. Nothing here generalises to another "
                 "instance without re-running it there.")
    f.limitation("Section C reads group_access from the modulestore. If enrollment-track "
                 "gating is applied at render time instead, this probe cannot see it — "
                 "compare against the Block Structure API called as an audit user.")

    print("# CourseMate — Probe 7\n")
    print(environment_block())
    print(f.render())


if __name__ == "__main__":
    main()
