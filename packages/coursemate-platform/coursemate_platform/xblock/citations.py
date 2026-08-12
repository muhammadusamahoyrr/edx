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

#: Marked sentences stored per answer. Same bound-the-state reason as the
#: citation cap: a verifier that flagged every sentence of a long answer must not
#: be able to grow `user_state` without limit.
MAX_UNSUPPORTED_PER_TURN = 8

_MAX_USAGE_KEY = 255
_MAX_DISPLAY_NAME = 200
_MAX_URL = 500
#: One flagged sentence. Generous enough for real prose, bounded because this is
#: browser-supplied text heading into per-student storage.
_MAX_SENTENCE = 500


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


def clean_unsupported(raw: object) -> list[str]:
    """The sentences an answer could not ground, kept with the turn.

    Same untrusted-input reasoning as `clean_citations`: `persist_turn` is a
    handler the student's own page calls, so this list arrives from the browser
    and is re-rendered on every page load.

    **Why persist these at all.** `UNSUPPORTED_CLAIM` marks existed only for the
    length of the live stream, so a refresh removed the warning and left the
    sentence — which is worse than never having warned. §8.5's promise is to mark
    a doubtful sentence and never silently rewrite what the student has read; an
    unmark on reload breaks that promise in the direction that manufactures
    confidence. It is the same defect `clean_citations` was written to fix, and
    the argument is stronger here: a lost citation weakens an answer's support, a
    lost warning invents it.

    Never raises. It runs after every answer, and an exception here would lose
    the student's turn.
    """
    if not isinstance(raw, list):
        return []

    cleaned: list[str] = []
    for item in raw[:MAX_UNSUPPORTED_PER_TURN]:
        if not isinstance(item, str):
            continue
        sentence = item.strip()[:_MAX_SENTENCE]
        if sentence:
            cleaned.append(sentence)
    return cleaned
