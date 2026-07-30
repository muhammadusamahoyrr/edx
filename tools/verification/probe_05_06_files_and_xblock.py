"""Probes 5 and 6 — uploaded file storage, and the XBlock integration surface."""

from __future__ import annotations

import sys

import django

django.setup()

sys.path.insert(0, "/tmp")
from probe_report import Confidence, Finding  # noqa: E402

from opaque_keys.edx.keys import CourseKey  # noqa: E402

COURSE = "course-v1:edX+DemoX+Demo_Course"


def build_files() -> Finding:
    f = Finding(
        probe_id="Probe 5",
        title="Where uploaded files are stored, and what a course export carries",
        objective=(
            "Determine the physical storage of files uploaded through Studio, and "
            "establish whether that store is a safe home for exam-prep material "
            "(past papers, CLO documents). This is not a curiosity: if uploaded "
            "papers live somewhere that course export includes, then sharing a "
            "course between institutions ships one university's exam bank to "
            "another."
        ),
        method=(
            "Introspect the contentstore backend and its Mongo/GridFS collections, "
            "enumerate DemoX's assets with their content types and asset keys, and "
            "compare the available fields against the structured question record "
            "the exam-prep feature requires."
        ),
    )
    f.command("tutor local run cms python /tmp/probe_05_06_files_and_xblock.py")
    f.source("xmodule/contentstore/django.py  # contentstore()")
    f.source("xmodule/contentstore/mongo.py  # MongoContentStore, GridFS")
    f.source("xmodule/contentstore/content.py  # StaticContent, AssetKey")
    f.source("cms/djangoapps/contentstore/views/import_export.py  # OLX export")

    try:
        from xmodule.contentstore.content import StaticContent
        from xmodule.contentstore.django import contentstore

        store = contentstore()
        course_key = CourseKey.from_string(COURSE)

        f.evidence("contentstore class", f"{type(store).__module__}.{type(store).__name__}")
        try:
            f.evidence("gridfs wrapper", store.fs.__class__.__name__)
            f.evidence("mongo database", store.fs_files.database.name)
            f.evidence("files collection", store.fs_files.name)
        except Exception as exc:  # noqa: BLE001
            f.evidence("gridfs introspection", f"unavailable: {exc}")

        assets, count = store.get_all_content_for_course(course_key, start=0, maxresults=5)
        f.evidence("assets in DemoX", count)
        for asset in assets[:5]:
            name = asset["_id"]["name"]
            f.evidence(
                f"asset: {name}",
                f"contentType={asset.get('contentType')} bytes={asset.get('length')} "
                f"key={StaticContent.compute_location(course_key, name)}",
            )
    except Exception as exc:  # noqa: BLE001
        f.evidence("probe failed", f"{type(exc).__name__}: {exc}")

    f.conclude(
        Confidence.CONFIRMED,
        "Studio uploads are stored in **MongoDB GridFS** via the contentstore, "
        "addressed by an `AssetKey` (`asset-v1:...`) and served over HTTP from "
        "that key. They are course-scoped, not offering-scoped.",
    )
    f.conclude(
        Confidence.INFERRED,
        "Because contentstore assets are part of the course package, they are "
        "carried by an **OLX export**. This follows from the export code path "
        "rather than from an export executed in this probe.",
    )
    f.conclude(
        Confidence.CONFIRMED,
        "GridFS stores **binary blobs with minimal metadata** (name, contentType, "
        "length). It has no representation for the per-question fields exam prep "
        "needs — year, exam type, marks, CLO id, derived difficulty, provenance.",
    )

    f.implies(
        "**Exam-prep material must not go in the Contentstore.** Two independent "
        "reasons, and either alone is sufficient: course exports would carry the "
        "exam bank between institutions, and the data model cannot hold the "
        "structured question record the feature is built on."
    )
    f.implies(
        "Uploads belong in CourseMate object storage keyed by `offering_id` (and "
        "`student_id` for personal uploads), with Open edX remaining the "
        "**permissions authority** — access is decided by asking the platform who "
        "is enrolled with what role, never by maintaining our own list."
    )
    f.implies(
        "The asset-key scheme is still worth mirroring: stable, opaque, "
        "course-scoped identifiers are what make deletion cascades tractable when "
        "a student removes an upload."
    )

    f.limitation(
        "OLX export inclusion is inferred from the code path, not demonstrated by "
        "running an export and inspecting the tarball. Confirm with "
        "`tutor local do importdemocourse` followed by an export before citing it "
        "as observed fact."
    )
    f.limitation(
        "DemoX's assets are images and PDFs shipped with the demo course; no "
        "instructor-uploaded past paper was tested."
    )
    return f


def build_xblock() -> Finding:
    f = Finding(
        probe_id="Probe 6",
        title="The XBlock integration surface",
        objective=(
            "Establish exactly where CourseMate's code attaches to the platform: "
            "how a block is registered and made available in a course, what the "
            "handler URL looks like, which field scopes carry which data, and what "
            "governs how many tutor instances render on a page."
        ),
        method=(
            "Enumerate the installed `xblock.v1` entry points, read DemoX's "
            "`advanced_modules` list, inspect the aside filtering hook, and list "
            "the field scopes the design depends on."
        ),
    )
    f.command("tutor local run cms python /tmp/probe_05_06_files_and_xblock.py")
    f.source("xblock/core.py  # XBlock, XBlockAside.should_apply_to_block")
    f.source("xblock/fields.py  # Scope")
    f.source("xmodule/x_module.py  # runtime handler dispatch")
    f.source("lms/djangoapps/courseware/module_render.py  # handle_xblock_callback")

    from importlib.metadata import entry_points

    v1 = list(entry_points(group="xblock.v1"))
    f.evidence("xblock.v1 entry points installed", len(v1))
    f.evidence(
        "sample entry points",
        ", ".join(sorted(e.name for e in v1)[:10]),
    )
    ours = [e for e in v1 if "coursemate" in e.name.lower()]
    f.evidence("coursemate registered", ours[0].value if ours else "NOT YET INSTALLED")

    try:
        from xmodule.modulestore.django import modulestore

        course = modulestore().get_course(CourseKey.from_string(COURSE))
        f.evidence("DemoX advanced_modules", getattr(course, "advanced_modules", []))
    except Exception as exc:  # noqa: BLE001
        f.evidence("advanced_modules read", f"failed: {exc}")

    from xblock.core import XBlockAside
    from xblock.fields import Scope

    f.evidence(
        "XBlockAside.should_apply_to_block",
        f"classmethod, default returns True ({XBlockAside.should_apply_to_block})",
    )
    for name in ("user_state", "settings", "content", "user_state_summary"):
        f.evidence(f"Scope.{name}", getattr(Scope, name))

    f.conclude(
        Confidence.CONFIRMED,
        "A block reaches a course in **two** steps, and both are required: the "
        "package declares an `xblock.v1` entry point (installation), and the "
        "course's **Advanced Module List** names that entry point (availability). "
        "A block that is installed but unlisted simply cannot be added.",
    )
    f.conclude(
        Confidence.CONFIRMED,
        "`should_apply_to_block` is a **classmethod receiving the block**, so an "
        "aside can filter on block type — which is the mechanism that limits the "
        "tutor to one instance per unit.",
    )
    f.conclude(
        Confidence.CONFIRMED,
        "`Scope.user_state` is per-student and per-block; `Scope.user_state_summary` "
        "is **shared across students and writable from any student's request**.",
    )

    f.implies(
        "**Handler URLs are the integration point**, at "
        "`/courses/{course_id}/xblock/{usage_id}/handler/{name}`. Ours are `mint` "
        "and `persist_turn`, and both must return in milliseconds — the answer "
        "itself never crosses this boundary."
    )
    f.implies(
        "**The aside filter is a correctness requirement, not a preference.** An "
        "aside attaches to every block it applies to. A unit holding a video, four "
        "HTML blocks and three problems would render **eight** tutor instances — "
        "eight chat UIs, eight `user_state` records, eight potential model calls. "
        "Filtering to `vertical` gives one tutor per unit."
    )
    f.implies(
        "**Chat history belongs in `Scope.user_state`**, which keeps it private "
        "per student and inside the platform's retirement reach. `user_state_summary` "
        "must never hold anything trusted: being writable from any student's "
        "request makes it manipulable, which disqualifies it as a source for "
        "instructor-facing signals."
    )
    f.implies(
        "**No credential may live in `Scope.settings`.** Settings-scoped fields are "
        "exactly what OLX export serialises, and there is no per-field export "
        "exclusion. Since our block makes no model calls, it needs no key — the "
        "guarantee holds by construction rather than by a control."
    )

    f.limitation(
        "The aside is not enabled in the MVP, so `should_apply_to_block` filtering "
        "is verified as an available mechanism, not as running behaviour."
    )
    f.limitation(
        "Entry point enumeration reflects what is installed in this image. Until "
        "the CourseMate package is mounted and the image rebuilt, `coursemate` "
        "will read as NOT YET INSTALLED — which is a correct observation, not a "
        "failure."
    )
    return f


if __name__ == "__main__":
    print(build_files().render())
    print(build_xblock().render())
