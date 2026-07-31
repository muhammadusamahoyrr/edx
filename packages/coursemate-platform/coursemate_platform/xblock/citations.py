"""Citation sanitisation — pure, so it is testable without Django.

Extracted from `tutor_block.py` because that module imports `django.conf` at
import time, which made a function with no Django dependency untestable in the
fast suite and in CI. A pure validator sitting behind a framework import is a
validator nobody exercises.
"""

from __future__ import annotations

#: Citations stored per answer. A retrieval returning many chunks must not be
#: able to grow a student's `user_state` without bound.
MAX_CITATIONS_PER_TURN = 8

_MAX_USAGE_KEY = 255
_MAX_DISPLAY_NAME = 200
_MAX_URL = 500


def clean_citations(raw: object) -> list[dict]:
    """Keep only the three fields the UI renders, from a payload the browser sent.

    `persist_turn` is a handler the student's own page calls, so its body is
    **untrusted input** even though the citations originally came from us. Storing
    it verbatim would put arbitrary browser-supplied structure into `user_state`
    and re-render it on every page load — storage is a persistence point for
    whatever the browser sends.

    Note what this does **not** do: it does not scheme-check the URL. That belongs
    at render time, in the client's `safeHref()`, because the same check must also
    cover live citation frames streamed from the service — which never pass
    through this function at all. Validating in one place that covers both paths
    is better than validating twice and trusting neither completely.

    Never raises: this runs after every answer, and an exception here loses the
    student's turn.
    """
    if not isinstance(raw, list):
        return []

    cleaned: list[dict] = []
    for item in raw[:MAX_CITATIONS_PER_TURN]:
        if not isinstance(item, dict):
            continue
        usage_key = str(item.get("usage_key") or "")[:_MAX_USAGE_KEY]
        if not usage_key:
            # Without a usage_key a citation cannot be checked against anything,
            # which makes it decoration rather than attribution.
            continue
        cleaned.append(
            {
                "usage_key": usage_key,
                "display_name": str(item.get("display_name") or "")[:_MAX_DISPLAY_NAME],
                "url": str(item.get("url") or "")[:_MAX_URL],
            }
        )
    return cleaned
