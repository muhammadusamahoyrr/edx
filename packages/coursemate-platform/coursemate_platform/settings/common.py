"""Plugin settings, merged into both LMS and CMS at COMMON stage.

**These functions run during Django startup, inside the LMS and CMS.** Two rules
follow from that, and both were learned by breaking the platform:

1. **`ENV_TOKENS` does not exist here.** It is defined in `production.py`, later
   in the settings chain. Reading it at COMMON stage raises
   `AttributeError: partially initialized module 'lms.envs.common' has no
   attribute 'ENV_TOKENS'` and the platform fails to boot.

2. **Nothing here may raise.** A plugin that raises during settings loading takes
   the whole LMS down — for every course, including the ones that never enabled
   CourseMate. That is precisely the failure Principle 8 forbids: *CourseMate must
   not be able to slow down, block, or break any core Open edX action.*

So this module sets **plain, safe defaults only**. Environment overrides happen in
`production.py`, and validation happens where the value is used (at token-mint
time), never at import time.
"""


def plugin_settings(settings):
    """Safe defaults. No environment reads, no validation, no exceptions."""

    # --- the student hop (design §3.4) ---------------------------------------
    # Never an XBlock field: there is no mechanism that excludes a Scope.settings
    # field from OLX export, so a secret's only safe home is outside the course.
    # Empty default is deliberate — an unset key must disable the feature, not
    # crash the platform. mint_student_token() rejects a weak key at call time.
    settings.COURSEMATE_JWT_SIGNING_KEY = ""

    # Same-origin path routed at the ingress (§3.4 v8), never a second published
    # hostname. Keeps the browser same-origin while the request never enters an
    # LMS application process.
    settings.COURSEMATE_STREAM_PATH = "/coursemate/api/chat"

    # --- server-to-server (§3.4 hops 2 and 3) --------------------------------
    settings.COURSEMATE_SERVICE_URL = "http://coursemate:8000"
    settings.COURSEMATE_SERVICE_CREDENTIAL = ""

    # Background work, so this timeout protects a Celery worker rather than a
    # student request.
    settings.COURSEMATE_HTTP_TIMEOUT_SECONDS = 30
    settings.COURSEMATE_INGEST_BATCH_SIZE = 50

    # --- tenancy (§3.5) -------------------------------------------------------
    # Single-valued in the MVP; present from day one because retrofitting an
    # isolation key later is expensive and carrying an unused one is free.
    settings.COURSEMATE_TENANT = "default"

    # --- reconciliation sweep (§5.4) -----------------------------------------
    # The ONLY mitigation for unpublished content: openedx-events has no unpublish
    # event, so nothing tells us when an instructor unpublishes a unit. Without
    # this the tutor keeps citing content students can no longer see.
    #
    # Nightly leaves a window of up to one interval. That window is real and is
    # stated in the docs rather than hidden -- it cannot be closed without a
    # platform event.
    settings.COURSEMATE_RECONCILE_ENABLED = True
    settings.COURSEMATE_RECONCILE_HOUR = 3       # local time, off-peak
    settings.COURSEMATE_RECONCILE_MINUTE = 30

    if settings.COURSEMATE_RECONCILE_ENABLED:
        # Wrapped because of rule 2 above, which this function would otherwise
        # be the first to break: this is the only import in the module, and an
        # ImportError here takes down the LMS for every course on the instance.
        # A missing scheduler must cost us a nightly sweep, not the platform.
        try:
            from celery.schedules import crontab

            beat = getattr(settings, "CELERYBEAT_SCHEDULE", None)
            if beat is None:
                beat = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
            beat["coursemate-nightly-reconcile"] = {
                "task": "coursemate_platform.tasks.reconcile.reconcile_all",
                "schedule": crontab(
                    hour=settings.COURSEMATE_RECONCILE_HOUR,
                    minute=settings.COURSEMATE_RECONCILE_MINUTE,
                ),
            }
            settings.CELERY_BEAT_SCHEDULE = beat
            settings.CELERYBEAT_SCHEDULE = beat
        except Exception:  # noqa: BLE001 — see above; never fatal at settings time
            pass

    # --- designed but dormant -------------------------------------------------
    settings.COURSEMATE_ASIDE_ENABLED = False  # §3.1, vertical-scoped when it lands
