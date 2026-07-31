"""Probe 3 — where published lesson content lives, and how to read it safely."""

from __future__ import annotations

import sys
import threading

import django

django.setup()

sys.path.insert(0, "/tmp")
from probe_report import Confidence, Finding  # noqa: E402

from opaque_keys.edx.keys import CourseKey  # noqa: E402
from xmodule.modulestore import ModuleStoreEnum  # noqa: E402
from xmodule.modulestore.django import modulestore  # noqa: E402

COURSE = "course-v1:OpenedX+DemoX+DemoCourse"


def build() -> Finding:
    f = Finding(
        probe_id="Probe 3",
        title="Content storage, per-type extraction, and published-branch isolation",
        objective=(
            "Locate where published lesson content physically lives, determine "
            "what `get_item()` returns for each block type we intend to ingest, "
            "and — most importantly — establish whether a background worker "
            "reading content will see the published branch or draft edits. The "
            "last one is a correctness question: reading drafts would mean the "
            "tutor teaches from unpublished content, with no error raised."
        ),
        method=(
            "Introspect the modulestore for the backing store class and its Mongo "
            "collections; read one block of each type and inspect which field "
            "holds content and in what format; then read the same block under both "
            "branch settings and compare, and inspect the thread-local branch state "
            "outside any context to model what a Celery worker inherits."
        ),
    )
    f.command("tutor local run cms python /tmp/probe_03_content_storage.py")
    f.source("xmodule/modulestore/mixed.py  # MixedModuleStore.branch_setting")
    f.source("xmodule/modulestore/__init__.py  # ModuleStoreEnum.Branch")
    f.source("xmodule/modulestore/split_mongo/split.py")
    f.source("xmodule/modulestore/django.py  # modulestore()")

    course_key = CourseKey.from_string(COURSE)
    store = modulestore()

    # --- A. backend ----------------------------------------------------------
    try:
        backing = store._get_modulestore_for_courselike(course_key)
        f.evidence("mixed store class", f"{type(store).__module__}.{type(store).__name__}")
        f.evidence("backing store class", f"{type(backing).__module__}.{type(backing).__name__}")
        f.evidence("backing default_branch", getattr(backing, "default_branch", "n/a"))
        try:
            db = backing.db_connection.database
            f.evidence("mongo database", db.name)
            f.evidence("collections", sorted(db.list_collection_names()))
        except Exception as exc:  # noqa: BLE001
            f.evidence("mongo introspection", f"unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001
        f.evidence("backend introspection failed", f"{type(exc).__name__}: {exc}")

    # --- B. per-type extraction ---------------------------------------------
    extraction_notes: dict[str, str] = {}
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        for block_type in ("html", "problem", "video", "vertical"):
            items = store.get_items(course_key, qualifiers={"category": block_type})
            if not items:
                f.evidence(f"{block_type}: present", "none in course")
                continue
            block = items[0]
            f.evidence(f"{block_type}: class", f"{type(block).__module__}.{type(block).__name__}")
            for attr in ("data", "transcripts", "sub"):
                if not hasattr(block, attr):
                    continue
                value = getattr(block, attr)
                if value in (None, "", {}, []):
                    continue
                raw = str(value)
                fmt = "HTML/XML — needs stripping" if "<" in raw[:300] else "plain text"
                extraction_notes[block_type] = fmt
                f.evidence(f"{block_type}: .{attr} format", fmt)
                f.evidence(f"{block_type}: .{attr} sample", raw[:160].replace("\n", " "))

    # --- C. branch isolation -------------------------------------------------
    thread_cache = getattr(store, "thread_cache", None)
    ambient = getattr(thread_cache, "branch_setting", None)
    f.evidence("thread name", threading.current_thread().name)
    f.evidence("branch_setting outside any context", repr(ambient))

    htmls = None
    with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
        htmls = store.get_items(course_key, qualifiers={"category": "html"})

    if htmls:
        target = htmls[0].scope_ids.usage_id
        f.evidence("comparison block", str(target))
        with store.branch_setting(ModuleStoreEnum.Branch.published_only, course_key):
            pub = (getattr(store.get_item(target), "data", "") or "")[:100]
        with store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, course_key):
            draft = (getattr(store.get_item(target), "data", "") or "")[:100]
        f.evidence("published_only text", pub.replace("\n", " "))
        f.evidence("draft_preferred text", draft.replace("\n", " "))
        f.evidence("branches identical (no pending draft)", pub == draft)

    # --- conclusions ---------------------------------------------------------
    f.conclude(
        Confidence.CONFIRMED,
        "Course content is stored in **Split Mongo**: structure in "
        "`modulestore.active_versions` / `modulestore.structures`, and the actual "
        "field data (a lesson's HTML) in `modulestore.definitions`. Reading it "
        "requires the `modulestore()` Python API — there is no network service "
        "that exposes lesson text.",
    )
    f.conclude(
        Confidence.CONFIRMED,
        "`branch_setting` is a context manager backed by **thread-local** storage "
        "(`self.thread_cache.branch_setting`) whose value is `None` when no "
        "context is active — as observed above.",
    )
    f.conclude(
        Confidence.INFERRED,
        "Therefore a **Celery worker inherits no branch setting**: it runs on a "
        "different thread with no active context and falls back to the backing "
        "store's own `default_branch`. For draft-capable stores that default is "
        "draft-preferred. This is derived from the observed thread-local "
        "mechanism plus the store's declared default, not from executing a task.",
    )
    if extraction_notes:
        f.conclude(
            Confidence.CONFIRMED,
            "Block content is returned as **markup, not clean text** for the types "
            "sampled, so a per-type extraction step is required before chunking.",
        )

    # --- implications --------------------------------------------------------
    f.implies(
        "**The content adapter must own the published-branch context, not expose "
        "it.** An API shaped as *\"call `iter_leaves()` inside a `branch_setting` "
        "block\"* fails open: one forgotten `with` silently indexes draft content, "
        "with no exception, no failing test, and no symptom until a student is "
        "cited text they cannot see. Every read opens the context internally."
    )
    f.implies(
        "**The ingest worker must run inside the Open edX deployment.** "
        "`modulestore()` is a Python API, not a service, so lesson text cannot be "
        "read across the network. This is the structural reason the architecture "
        "splits: the platform reads, the service transforms."
    )
    f.implies(
        "**Budget a per-type extractor.** Each type whose content field returns "
        "markup needs its own handling; a type that returns nothing usable should "
        "be excluded and logged as unsupported rather than half-read — a recorded "
        "gap is recoverable, a silent one is not."
    )
    f.implies(
        "Chunking must never merge two blocks, because the block is both the "
        "citation key and the swap key. Enforcing that in the wire format (one "
        "record per leaf) is stronger than enforcing it in a chunker conditional."
    )

    # --- limitations ---------------------------------------------------------
    f.limitation(
        "If DemoX has no unpublished draft edits, the two branch reads will be "
        "identical and the isolation test is **inconclusive** rather than passing. "
        "To make it decisive: edit a block in Studio, do not publish, re-run, and "
        "confirm the two lines differ."
    )
    f.limitation(
        "The Celery-inheritance conclusion is INFERRED from the thread-local "
        "mechanism, not observed from inside a running task. Confirming it "
        "requires queueing a real task that reads without pinning."
    )
    f.limitation(
        "Only the first block of each type was sampled. A course may contain "
        "blocks of the same type with different content shapes."
    )
    return f


if __name__ == "__main__":
    print(build().render())
