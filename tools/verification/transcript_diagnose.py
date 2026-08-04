"""Why does `get_transcript` return nothing for videos that have a pointer?

Probe 7 established the fact: 10 published videos in DemoX, all carrying a
transcript pointer, none yielding text. It could not say *why*, because
`_video_transcript` catches the exception and logs it at INFO — the fail-closed
default that hides its own cause.

This runs the same call with NOTHING caught, so the exception type and message
are visible. It is separate from probe 7 on purpose: probe 7 must stay safe to
run unattended, and a probe that lets exceptions escape is not.

    docker cp tools/verification/transcript_diagnose.py <cms>:/openedx/probes/
    docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production <cms> \
        python /openedx/probes/transcript_diagnose.py
"""

from __future__ import annotations

import os
import traceback

import django

django.setup()

COURSE = os.environ.get("COURSEMATE_PROBE_COURSE", "course-v1:OpenedX+DemoX+DemoCourse")
SAMPLE = int(os.environ.get("COURSEMATE_PROBE_SAMPLE", "3"))


def main() -> None:
    from opaque_keys.edx.keys import CourseKey
    from xmodule.modulestore import ModuleStoreEnum
    from xmodule.modulestore.django import modulestore

    from coursemate_platform.adapters import content_adapter

    get_transcript = content_adapter._transcript_resolver()  # noqa: SLF001
    print(f"resolver: {getattr(get_transcript, '__module__', None)}")

    course_key = CourseKey.from_string(COURSE)
    store = modulestore()

    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        videos = store.get_items(course_key, qualifiers={"category": "video"})
        print(f"published videos: {len(videos)}\n")

        for block in videos[:SAMPLE]:
            print("=" * 72)
            print("usage_key      :", block.scope_ids.usage_id)
            print("display_name   :", getattr(block, "display_name", None))
            print("sub            :", repr(getattr(block, "sub", None)))
            print("transcripts    :", repr(getattr(block, "transcripts", None)))
            print("edx_video_id   :", repr(getattr(block, "edx_video_id", None)))
            print("youtube_id_1_0 :", repr(getattr(block, "youtube_id_1_0", None)))

            # What the platform itself thinks is available. `get_transcript`
            # calls this first, so a wrong answer here explains everything after.
            try:
                info = block.get_transcripts_info()
                print("transcripts_info:", info)
            except Exception as exc:  # noqa: BLE001
                print("transcripts_info FAILED:", type(exc).__name__, exc)
                info = None

            try:
                lang = block.get_default_transcript_language(info) if info else None
                print("default language:", repr(lang))
            except Exception as exc:  # noqa: BLE001
                print("default language FAILED:", type(exc).__name__, exc)

            # The real call, uncaught.
            for fmt in ("txt", "sjson"):
                try:
                    content, filename, mimetype = get_transcript(block, output_format=fmt)
                    print(f"get_transcript({fmt}) OK -> {len(content)} chars, "
                          f"file={filename}, mime={mimetype}")
                    print("   sample:", content[:160].replace("\n", " "))
                except Exception as exc:  # noqa: BLE001
                    print(f"get_transcript({fmt}) RAISED {type(exc).__name__}: {exc}")
                    traceback.print_exc(limit=3)
            print()


if __name__ == "__main__":
    main()
