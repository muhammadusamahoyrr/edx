"""Counters for the handful of events worth watching in production.

**The gap this closes.** Every quality claim this project makes is measured — but
by `eval/`, offline, against a gold set. The *running* instance measured nothing
about itself, so "what is the abstention rate this week", "is the cache ever
hitting", "how often is the provider failing" could only be answered by
re-running the harness against a copy of the index. Phase D2 spent hours
diagnosing by hand what two counters would have shown.

**Deliberately tiny.** A dict of integers and a Prometheus text rendering, no
dependency. `prometheus_client` would be the obvious reach, and it buys a
registry, label handling and a WSGI app this service does not need — the whole
requirement is six numbers that only go up.

**Process-local, and that is correct rather than a limitation.** Prometheus
scrapes each instance and sums across them; a counter shared through Redis would
be a distributed write on the request path to answer a question the scraper
already answers. It does mean a restart resets them, which is exactly what
`_total` counters are expected to do — scrapers handle resets.

**Nothing here is student data.** Six aggregate integers, no identifiers, no
question text. The endpoint still carries the service credential, because an
aggregate is not nothing: request volume is a signal about an institution.
"""

from __future__ import annotations

import threading

#: Every counter, declared with the help text a scraper will show. Declaring the
#: full set here — rather than creating keys on first use — means a counter that
#: is never incremented still renders as 0, which is the difference between
#: "nothing happened" and "the code path is not wired".
_COUNTERS: dict[str, str] = {
    "chat_requests_total": "Chat answers requested, before any gate.",
    "abstentions_total": "Answers refused by the confidence gate (§8.5).",
    "cache_hits_total": "First-turn responses served from cache.",
    "cache_misses_total": "First-turn responses that had to be generated.",
    "budget_refusals_total": "Requests refused by the daily token ceiling.",
    "provider_failures_total": "Generations that failed or timed out upstream.",
    #: **The blind spot `provider_failures_total` cannot see.** That counter is
    #: incremented in `pipeline.py` only when an exception reaches the pipeline,
    #: and the LiteLLM Router swallows a provider failure whenever a fallback
    #: succeeds. Measured against a real outage (ADR-0001, phase 4): the degraded
    #: step moved it by 0 and only the total outage moved it by 1.
    #:
    #: So a primary degrading on EVERY request was invisible, while the counter's
    #: name suggested otherwise — the worst combination, because a dashboard
    #: showing zero reads as health. This one moves whenever a deployment other
    #: than the primary answered, which is the same condition that raises the
    #: DEGRADED frame the student sees.
    "degraded_answers_total": "Answers served by a fallback deployment, not the primary.",
    #: The outline path splits in two, and the split is the thing worth watching.
    #: Before these existed, an outline answer, a fallthrough and an ordinary
    #: question were indistinguishable — all three moved `chat_requests_total`
    #: and nothing else, so "is the outline path ever used" and "how often does a
    #: course have no author summaries" were unanswerable from a running
    #: instance.
    "outline_answers_total": "Outline questions answered deterministically from author summaries.",
    #: A high ratio against `outline_answers_total` means most courses have no
    #: author-written overview — which is the measurement that decides whether
    #: the structural fallback is worth building.
    "outline_fallthrough_total": "Outline questions with no usable summary, sent to ordinary retrieval.",
}

_lock = threading.Lock()
_values: dict[str, int] = dict.fromkeys(_COUNTERS, 0)


def increment(name: str, amount: int = 1) -> None:
    """Add to a counter. Unknown names raise — a typo must not create a metric
    nobody is reading, which is how a dashboard ends up quietly measuring
    nothing."""
    if name not in _COUNTERS:
        raise KeyError(f"unknown counter {name!r}; declare it in metrics._COUNTERS")
    with _lock:
        _values[name] += amount


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_values)


def reset_for_tests() -> None:
    with _lock:
        _values.update(dict.fromkeys(_COUNTERS, 0))


def render() -> str:
    """Prometheus text exposition, hand-written.

    The format is four lines per metric and has been stable for a decade; a
    library to emit it would be a dependency to concatenate strings.
    """
    current = snapshot()
    lines: list[str] = []
    for name, help_text in _COUNTERS.items():
        metric = f"coursemate_{name}"
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{metric} {current[name]}")
    return "\n".join(lines) + "\n"
