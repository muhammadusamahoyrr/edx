"""Plugin settings, merged into both LMS and CMS.

Loaded via `plugin_app[PluginSettings.CONFIG]` in apps.py, for both project types.
"""


def plugin_settings(settings):
    """Called by edx-django-utils with the Django settings module."""

    # --- the student hop (§3.4) ----------------------------------------------
    # Never an XBlock field: there is no mechanism that excludes a Scope.settings
    # field from OLX export, so the only safe home for a secret is somewhere the
    # course package cannot reach (§10.4).
    settings.COURSEMATE_JWT_SIGNING_KEY = settings.ENV_TOKENS.get(
        "COURSEMATE_JWT_SIGNING_KEY", ""
    )

    # Same-origin path, routed at the ingress — never a second published
    # hostname (§3.4, v8). Keeps the browser same-origin (no CORS) while the
    # request never enters an LMS application process.
    settings.COURSEMATE_STREAM_PATH = settings.ENV_TOKENS.get(
        "COURSEMATE_STREAM_PATH", "/coursemate/api/chat"
    )

    # --- server-to-server (§3.4 hops 2 and 3) --------------------------------
    # A separate credential from the student path, so a leaked student token
    # cannot write to the index.
    settings.COURSEMATE_SERVICE_URL = settings.ENV_TOKENS.get(
        "COURSEMATE_SERVICE_URL", "http://coursemate:8000"
    )
    settings.COURSEMATE_SERVICE_CREDENTIAL = settings.ENV_TOKENS.get(
        "COURSEMATE_SERVICE_CREDENTIAL", ""
    )

    # Ingest and invalidation are background work, so the timeout protects the
    # Celery worker rather than a student request.
    settings.COURSEMATE_HTTP_TIMEOUT_SECONDS = 30
    settings.COURSEMATE_INGEST_BATCH_SIZE = 50

    # --- tenancy (§3.5) -------------------------------------------------------
    # Single-valued in the MVP. Present from day one because retrofitting an
    # isolation key later is expensive and carrying an unused one is free.
    settings.COURSEMATE_TENANT = settings.ENV_TOKENS.get("COURSEMATE_TENANT", "default")

    # --- designed but dormant -------------------------------------------------
    settings.COURSEMATE_ASIDE_ENABLED = False  # §3.1, vertical-scoped when it lands
