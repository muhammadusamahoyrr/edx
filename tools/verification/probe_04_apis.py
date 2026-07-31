"""Probe 4 — Course Blocks, Completion and Enrollment APIs, authenticated.

    tutor local run lms python /openedx/probes/probe_04_apis.py

Authenticated with a real OAuth2 token, not Django test-client shortcuts, because
the question is whether *our service* can call these across the network — and a
test client would answer a different question.

Why each one matters to the design:

  Course Blocks    structure and display names for citations (§8.5 requires every
                   answer to cite its source block, and a citation needs a title
                   and a deep link, not just a usage_key).
  Enrollment       the authority for "is this student in this course". §10.1 says
                   authorization is INHERITED, never reinvented — we never keep
                   our own enrollment list, so this endpoint is the boundary's
                   source of truth on every tool call (§6.5).
  Completion       feeds get_student_progress, and would have fed struggle signals
                   had they not been deferred (§9.3). Note the design's warning:
                   NOT Scope.user_state_summary, which is shared-scope and
                   writable from any student's request, hence manipulable.
"""

from __future__ import annotations

import json

import django

django.setup()

import requests  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402

LMS = "http://localhost:8000"
COURSE = "course-v1:OpenedX+DemoX+DemoCourse"


def issue_token(username: str = "admin") -> str | None:
    """Mint a real OAuth2 access token the way an external service would."""
    from oauth2_provider.models import get_application_model, get_access_token_model
    from django.utils import timezone
    from datetime import timedelta
    import secrets

    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        print(f"  no user '{username}'")
        return None

    Application = get_application_model()
    AccessToken = get_access_token_model()

    app, _ = Application.objects.get_or_create(
        name="coursemate-probe",
        defaults={
            "user": user,
            "client_type": "confidential",
            "authorization_grant_type": "client-credentials",
        },
    )
    token = AccessToken.objects.create(
        user=user,
        application=app,
        token=secrets.token_urlsafe(32),
        expires=timezone.now() + timedelta(hours=1),
        scope="read write",
    )
    return token.token


def call(name: str, url: str, token: str) -> None:
    print(f"\n  {name}")
    print(f"    GET {url}")
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        print(f"    HTTP {response.status_code}")
        if response.ok:
            body = response.json()
            text = json.dumps(body, indent=6)
            print("      " + "\n      ".join(text.splitlines()[:14]))
        else:
            print(f"    body: {response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {exc}")


def main() -> None:
    print("=" * 72)
    print("AUTHENTICATED API PROBE")
    print("=" * 72)

    token = issue_token()
    if not token:
        return
    print(f"  minted OAuth2 token: {token[:12]}...")

    # --- Course Blocks -------------------------------------------------------
    # all_blocks=true + depth=all is the shape that returns a whole course tree.
    call(
        "COURSE BLOCKS  (structure + display_name for citations)",
        f"{LMS}/api/courses/v2/blocks/?course_id={COURSE}"
        "&depth=all&all_blocks=true&requested_fields=display_name,block_type,children",
        token,
    )

    # --- Enrollment ----------------------------------------------------------
    call(
        "ENROLLMENT  (the authority the boundary re-checks on every call)",
        f"{LMS}/api/enrollment/v1/enrollment",
        token,
    )

    # --- Completion ----------------------------------------------------------
    call(
        "COMPLETION  (progress; feeds get_student_progress)",
        f"{LMS}/api/completion/v1/course/{COURSE}/",
        token,
    )

    print()
    print("=" * 72)
    print("WHERE THESE LIVE IN edx-platform")
    print("=" * 72)
    for label, module in [
        ("Course Blocks", "lms.djangoapps.course_api.blocks"),
        ("Enrollment", "openedx.core.djangoapps.enrollments"),
        ("Completion", "completion"),
    ]:
        try:
            imported = __import__(module, fromlist=["__file__"])
            print(f"  {label:15s} {imported.__file__}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:15s} (not importable: {exc})")


if __name__ == "__main__":
    main()
