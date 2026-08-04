"""Attach a transcript to ONE DemoX video, so the video path can be proven.

Probe 7 established that DemoX ships edx-val transcript ROWS whose `.srt` files
were never included, so every video yields nothing. That makes the extraction
path unprovable on this course — not broken, untested, which is a different
thing and a worse place to leave it.

This repairs exactly one video, through `edxval.api.create_or_update_video_transcript`
rather than by writing a file into the media volume. The distinction matters: the
API writes the row AND the file through Django storage, which is the same path a
real transcript upload takes. Poking the file directly would test the filesystem
and tell us nothing about whether the platform can find it.

**Writes course data.** One video, one language, clearly marked. To undo:

    from edxval.api import delete_video_transcript
    delete_video_transcript(video_id="<VIDEO_ID>", language_code="en")

    docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production <cms> \
        python /openedx/probes/add_test_transcript.py
"""

from __future__ import annotations

import os

import django

django.setup()

COURSE = os.environ.get("COURSEMATE_PROBE_COURSE", "course-v1:OpenedX+DemoX+DemoCourse")

#: Deliberately about the video's actual subject, so retrieval quality on it is
#: meaningful rather than a keyword trick. The phrase "campus-wide deployments"
#: appears nowhere else in DemoX, which is what lets the retrieval check below
#: prove the hit came from THIS chunk and not an html page about the same topic.
SRT = """1
00:00:01,000 --> 00:00:06,000
The Open edX platform powers online learning for universities, companies and
governments around the world.

2
00:00:06,000 --> 00:00:12,000
It began as the software behind edX dot org, and was released as open source so
that any institution could run its own site.

3
00:00:12,000 --> 00:00:19,000
Today it supports everything from a single instructor teaching one course to
campus-wide deployments serving hundreds of thousands of learners.

4
00:00:19,000 --> 00:00:26,000
Because the platform is modular, teams can extend it with their own XBlocks and
plugins without forking the core codebase.

5
00:00:26,000 --> 00:00:32,000
That combination of openness and extensibility is what gives the platform its
reach.
"""


def main() -> None:
    from django.core.files.base import ContentFile
    from opaque_keys.edx.keys import CourseKey
    from xmodule.modulestore import ModuleStoreEnum
    from xmodule.modulestore.django import modulestore

    from coursemate_platform.adapters import content_adapter

    course_key = CourseKey.from_string(COURSE)
    store = modulestore()

    # Pick the first published video that edx-val actually knows about. A video
    # with no edx_video_id has no VAL record to attach to, and would need the
    # contentstore path instead.
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        target = None
        for block in store.get_items(course_key, qualifiers={"category": "video"}):
            if getattr(block, "edx_video_id", None):
                target = block
                break

    if target is None:
        print("FAIL: no published video with an edx_video_id in this course.")
        return

    video_id = target.edx_video_id
    print(f"target block   : {target.scope_ids.usage_id}")
    print(f"display_name   : {target.display_name}")
    print(f"edx_video_id   : {video_id}")

    from edxval.api import create_or_update_video_transcript

    url = create_or_update_video_transcript(
        video_id=video_id,
        language_code="en",
        metadata={
            "file_format": "srt",       # TranscriptFormat.SRT
            "language_code": "en",
            "provider": "Custom",       # TranscriptProviderType.CUSTOM
        },
        file_data=ContentFile(SRT.encode("utf-8"), name=f"{video_id}-en.srt"),
    )
    print(f"transcript url : {url}")

    # The point of the exercise: does OUR extractor now return text? Calling the
    # shipped function rather than get_transcript directly, because the thing
    # under test is the adapter, not the platform.
    text = content_adapter._video_transcript(target)  # noqa: SLF001
    print(f"\n_video_transcript -> {len(text)} chars")
    print(f"sample: {text[:220]}")

    if not text.strip():
        print("\nFAIL: transcript attached but the adapter still returns nothing.")
        return

    # And does the walk now emit it as a leaf? A transcript the extractor can
    # read but the walk drops would still never reach the index.
    leaves = list(content_adapter.iter_course_leaves(course_key))
    videos = [leaf for leaf in leaves if leaf.block_type == "video"]
    print(f"\nleaves total   : {len(leaves)}")
    print(f"video leaves   : {len(videos)}")
    for leaf in videos:
        print(f"   {leaf.display_name!r} -> {len(leaf.text)} chars")

    print("\nPASS" if videos else "\nFAIL: extractor works but the walk yields no video leaf")


if __name__ == "__main__":
    main()
