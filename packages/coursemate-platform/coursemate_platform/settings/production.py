"""Production overrides — reads the deployment's environment.

`ENV_TOKENS` exists at this stage (unlike COMMON), so this is where environment
values are read. It is still **not** a place to raise: this code runs during LMS
and CMS startup, and an exception here stops the platform for every course on the
instance, including those that never enabled CourseMate.

An earlier version of this file raised `RuntimeError` when the signing key was
unset, reasoning that an unsigned student hop should fail loudly. The instinct was
right and the placement was wrong — *loud* must not mean *fatal to the host
platform*. The design's own Principle 8 settles it:

    CourseMate must not be able to slow down, block, or break any core Open edX
    action.

So an unset key **disables the feature and logs**, and the refusal happens at the
point of use: `mint_student_token()` raises `WeakSigningKey` rather than signing
with a secret too short to be worth having (RFC 7518 §3.2).
"""

import logging

from .common import plugin_settings as _common

log = logging.getLogger(__name__)


def plugin_settings(settings):
    _common(settings)

    env = getattr(settings, "ENV_TOKENS", {}) or {}

    settings.COURSEMATE_JWT_SIGNING_KEY = env.get(
        "COURSEMATE_JWT_SIGNING_KEY", settings.COURSEMATE_JWT_SIGNING_KEY
    )
    settings.COURSEMATE_STREAM_PATH = env.get(
        "COURSEMATE_STREAM_PATH", settings.COURSEMATE_STREAM_PATH
    )
    settings.COURSEMATE_SERVICE_URL = env.get(
        "COURSEMATE_SERVICE_URL", settings.COURSEMATE_SERVICE_URL
    )
    settings.COURSEMATE_SERVICE_CREDENTIAL = env.get(
        "COURSEMATE_SERVICE_CREDENTIAL", settings.COURSEMATE_SERVICE_CREDENTIAL
    )
    settings.COURSEMATE_TENANT = env.get("COURSEMATE_TENANT", settings.COURSEMATE_TENANT)

    # Visible in the logs, harmless to the platform. The tutor simply cannot mint
    # a token until an operator supplies a key, which is the correct degraded
    # state: no answers, rather than unsigned answers.
    if not settings.COURSEMATE_JWT_SIGNING_KEY:
        log.warning(
            "CourseMate: COURSEMATE_JWT_SIGNING_KEY is unset. The tutor is "
            "disabled until it is configured; the platform is unaffected."
        )
