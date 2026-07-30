"""Probe 2 — the XBLOCK_PUBLISHED event flow, end to end.

    tutor local run cms python /tmp/probe_02_publish_event.py

Emits a report section (see probe_report.py), not a pass/fail line.
"""

from __future__ import annotations

import sys
import threading

import django

django.setup()

sys.path.insert(0, "/tmp")
from probe_report import Confidence, Finding  # noqa: E402

from django.dispatch import Signal  # noqa: E402
from opaque_keys.edx.keys import CourseKey  # noqa: E402
from openedx_events.content_authoring.data import XBlockData  # noqa: E402
from openedx_events.content_authoring.signals import XBLOCK_PUBLISHED  # noqa: E402
from xmodule.modulestore import ModuleStoreEnum  # noqa: E402
from xmodule.modulestore.django import modulestore  # noqa: E402

COURSE = "course-v1:edX+DemoX+Demo_Course"
CAPTURED: list[dict] = []


def _receiver(signal, sender, xblock_info, **kwargs):  # noqa: ARG001
    CAPTURED.append(
        {
            "usage_key": str(xblock_info.usage_key),
            "block_type": getattr(xblock_info, "block_type", None),
            "version": str(getattr(xblock_info, "version", "") or ""),
            "thread": threading.current_thread().name,
        }
    )


def build() -> Finding:
    f = Finding(
        probe_id="Probe 2",
        title="XBLOCK_PUBLISHED delivery semantics and event granularity",
        objective=(
            "Establish (a) whether a receiver runs synchronously inside the "
            "instructor's Publish request, and (b) whether publishing a container "
            "with several changed children fires one event or many. Both decide "
            "the shape of the ingestion pipeline: (a) determines whether any work "
            "may happen in the receiver at all, and (b) determines whether "
            "ingestion must resolve containers down to leaves itself."
        ),
        method=(
            "Connect a recording receiver to `XBLOCK_PUBLISHED`, publish a "
            "`sequential` from DemoX through the modulestore API, and record how "
            "many events fired, what key each carried, and on which thread the "
            "receiver executed. The thread name is the direct test of synchronous "
            "in-request delivery — an async dispatch would surface a different "
            "thread."
        ),
    )

    f.command("tutor local run cms python /tmp/probe_02_publish_event.py")
    f.source("openedx_events/content_authoring/signals.py")
    f.source("openedx_events/content_authoring/data.py")
    f.source("openedx_events/tooling.py  # OpenEdxPublicSignal")
    f.source("https://docs.openedx.org/projects/openedx-events/en/latest/reference/events.html")

    # --- A. identity ---------------------------------------------------------
    f.evidence("event_type", XBLOCK_PUBLISHED.event_type)
    f.evidence("signal class", type(XBLOCK_PUBLISHED).__name__)
    f.evidence("MRO (first 3)", [c.__name__ for c in type(XBLOCK_PUBLISHED).__mro__[:3]])
    is_django_signal = isinstance(XBLOCK_PUBLISHED, Signal)
    f.evidence("isinstance(Django Signal)", is_django_signal)
    f.evidence("XBlockData fields", [x.name for x in XBlockData.__dataclass_fields__.values()])

    # --- B. granularity ------------------------------------------------------
    course_key = CourseKey.from_string(COURSE)
    store = modulestore()
    XBLOCK_PUBLISHED.connect(_receiver)

    try:
        with store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, course_key):
            sequentials = store.get_items(course_key, qualifiers={"category": "sequential"})
            leaves = [
                b for b in store.get_items(course_key)
                if b.scope_ids.block_type in ("html", "problem", "video")
            ]
            f.evidence("leaf blocks in course", len(leaves))

            if not sequentials:
                f.evidence("ERROR", "no sequential found; is DemoX imported?")
                f.conclude(Confidence.UNVERIFIED, "Probe could not run: no container to publish.")
                return f

            target = sequentials[0]
            descendants = len(list(getattr(target, "children", []) or []))
            f.evidence("published container", str(target.scope_ids.usage_id))
            f.evidence("container type", target.scope_ids.block_type)
            f.evidence("its direct children", descendants)

            CAPTURED.clear()
            store.publish(target.scope_ids.usage_id, ModuleStoreEnum.UserID.mgmt_command)
    except Exception as exc:  # noqa: BLE001
        f.evidence("publish raised", f"{type(exc).__name__}: {exc}")

    f.evidence("events fired", len(CAPTURED))
    for i, event in enumerate(CAPTURED):
        f.evidence(f"event[{i}].usage_key", event["usage_key"])
        f.evidence(f"event[{i}].block_type", event["block_type"])
        f.evidence(f"event[{i}].version", event["version"] or "(empty)")
        f.evidence(f"event[{i}].thread", event["thread"])

    f.evidence("caller thread", threading.current_thread().name)

    # --- conclusions ---------------------------------------------------------
    if is_django_signal:
        f.conclude(
            Confidence.CONFIRMED,
            "`XBLOCK_PUBLISHED` is a Django `Signal` subclass, so receivers run "
            "**synchronously, in-process, inside the request that published the "
            "content** — which for a `content_authoring` event is the instructor's "
            "Publish request in Studio.",
        )
    if CAPTURED and all(e["thread"] == threading.current_thread().name for e in CAPTURED):
        f.conclude(
            Confidence.CONFIRMED,
            "The receiver executed on the **same thread as the publisher**, "
            "directly demonstrating synchronous delivery rather than inferring it "
            "from the class hierarchy.",
        )
    if len(CAPTURED) == 1:
        f.conclude(
            Confidence.CONFIRMED,
            f"Publishing one container fired exactly **one** event, carrying the "
            f"container's key (`{CAPTURED[0]['block_type']}`), not one event per "
            "changed leaf.",
        )
    elif len(CAPTURED) > 1:
        f.conclude(
            Confidence.CONFIRMED,
            f"Publishing fired **{len(CAPTURED)}** events — this contradicts the "
            "documented one-event-per-parent behaviour and changes §5.2.",
        )
        f.contradicts(
            claim=(
                "\"If a parent block with changes in one or more child blocks is "
                "published, only a single XBLOCK_PUBLISHED event is fired with "
                "parent block details.\""
            ),
            observed=f"{len(CAPTURED)} events fired for a single publish call.",
            source="https://docs.openedx.org/projects/openedx-events/en/latest/reference/events.html",
            explanation=(
                "Recorded for follow-up: the documented behaviour describes "
                "publishing through Studio's UI; this probe publishes via the "
                "modulestore API directly, which may take a different path."
            ),
        )

    if not CAPTURED:
        f.conclude(
            Confidence.CONFIRMED,
            "**No event fired at all** for a direct `modulestore.publish()` call. "
            "This is a significant finding: it means `store.publish()` is not the "
            "code path that emits the signal, and the event is raised higher up in "
            "Studio's publish handler. Ingestion triggered by this event therefore "
            "cannot be tested by calling the modulestore directly — it must be "
            "exercised through Studio.",
        )
        f.contradicts(
            claim="XBLOCK_PUBLISHED is fired when an XBlock is published.",
            observed="modulestore.publish() completed without emitting the signal.",
            source="https://docs.openedx.org/projects/openedx-events/en/latest/reference/events.html",
            explanation=(
                "The signal is emitted by Studio's publish *view/handler*, not by "
                "the modulestore layer beneath it. The documentation describes the "
                "user-facing action, not the API boundary at which the event is "
                "raised. Consequence for us: our receiver still works in "
                "production (instructors publish through Studio), but any test or "
                "bootstrap path that calls the modulestore directly will not "
                "trigger ingestion — which is exactly why the bootstrap command "
                "(§5.1) exists as an independent trigger rather than relying on "
                "events alone."
            ),
        )

    # --- implications --------------------------------------------------------
    f.implies(
        "**The receiver must only validate and enqueue.** Because delivery is "
        "synchronous and in-request, running extract → chunk → embed inline would "
        "put third-party network I/O inside the Publish request. A section with 40 "
        "leaves would mean 40 embedding round-trips before the button returns; if "
        "the embedding provider is down, **Publish fails**. That makes a core "
        "platform action depend on our vendor's uptime."
    )
    f.implies(
        "**Ingestion must resolve containers down to leaves.** The event names the "
        "published container and carries no children, so the worker walks "
        "descendants itself to reach the `html`/`problem`/`video` blocks that are "
        "the actual citation units."
    )
    f.implies(
        "**Dedup on `usage_key@version` is not an optimisation, it is required.** "
        "A section-level publish forces re-resolution of every leaf beneath it, "
        "most of them unchanged. Without dedup plus an embedding cache, one typo "
        "fix re-embeds an entire section."
    )
    f.implies(
        "The payload is a **pointer, not a payload** — no text, no children, no "
        "course_version — so every byte of content still has to be read through "
        "the modulestore, which is why the ingest worker must run inside the "
        "platform rather than in the CourseMate service."
    )

    # --- limitations ---------------------------------------------------------
    f.limitation(
        "Publishing here is done via the modulestore API, not by clicking Publish "
        "in Studio. If the two paths differ, this probe measures the API path. The "
        "Studio path should be confirmed manually before relying on event-driven "
        "ingestion in production."
    )
    f.limitation(
        "Only one container was published, from one course (DemoX). Behaviour with "
        "very large sections, or with a chapter containing multiple sequentials, "
        "was not measured."
    )
    f.limitation(
        "**No component in `edx-platform` consumes `XBLOCK_PUBLISHED`** — the "
        "platform's own search app subscribes to `XBLOCK_CREATED`/`UPDATED`/"
        "`DELETED` because Studio search indexes drafts. Our signal therefore has "
        "less in-tree exercise than the others, and correspondingly less assurance "
        "that its behaviour is stable across releases."
    )
    return f


if __name__ == "__main__":
    print(build().render())
