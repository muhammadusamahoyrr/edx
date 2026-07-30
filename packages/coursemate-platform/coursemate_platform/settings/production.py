"""Production overrides. Same keys, read from the deployment's environment."""

from .common import plugin_settings as _common


def plugin_settings(settings):
    _common(settings)
    # In production every one of these must be set; an empty signing key means
    # the student hop is unsigned, so it fails loudly at startup rather than
    # silently accepting anything (§3.4).
    if not settings.COURSEMATE_JWT_SIGNING_KEY:
        raise RuntimeError(
            "COURSEMATE_JWT_SIGNING_KEY is unset. The XBlock->service hop is the "
            "one hop Open edX does not secure for us; refusing to start unsigned."
        )
