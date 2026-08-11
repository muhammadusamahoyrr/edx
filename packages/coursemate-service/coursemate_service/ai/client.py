"""LiteLLM Router — the model abstraction layer (design §8.4).

Everything here is **configuration, not code**, and that is the point. The Router
natively provides `fallbacks`, `content_policy_fallbacks`, a per-error-type
`RetryPolicy`, and `allowed_fails` cooldowns that remove an unhealthy deployment
from the pool. Reimplementing any of that by hand would be a week of work and a
worse result.

Four rules keep the fallback chain from becoming a silent quality regression:

1. **Retry only what is retryable.** Timeouts, 429s and 5xx retry. A content
   policy refusal routes to `content_policy_fallbacks` rather than retrying — the
   model will refuse again. A malformed 400 fails fast.
2. **Cooldowns, not blind retries.** After `allowed_fails` a deployment leaves the
   pool, so an outage does not cost a full timeout on every request.
3. **Degradation is visible.** The answering provider is recorded and surfaced to
   the UI, otherwise an outage reads as "the tutor got worse this week".
4. **Grounding never relaxes on fallback.** The fallback model gets the same
   context and the same citation requirement. Falling back changes *who answers*,
   never *whether the answer must be grounded*.

---

**The bug that made rule 3 false, and rule 2 with it.** `fallback_model` used to
be registered as a SECOND deployment named `strong`. Deployments sharing a
`model_name` are not a priority chain — the Router load-balances across them.
Measured against litellm 1.94.1 with both providers healthy: **20 of 40 calls
went to the "fallback"**.

So the secondary vendor served half of all normal traffic. Answer quality and
spend split unpredictably between two providers, every other answer was marked
DEGRADED until "degraded" meant nothing, and the one job the setting existed for
— surviving a vendor outage — was not what it did. It was also invisible: both
providers answer fine, so nothing fails.

A fallback is now its own `model_name` and is reached through `fallbacks=`, which
is the mechanism that actually implements "use this one only when the other is
gone". The different-vendor deployment comes first in the chain and `cheap` last,
because `cheap` shares the primary's vendor and therefore its outage.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)


class NoModelConfigured(RuntimeError):
    """No provider is configured. The tutor is unavailable and says so."""


#: The logical deployment the pipeline asks for. Anything else answering means
#: the primary was unreachable, which is what the DEGRADED frame reports.
PRIMARY_DEPLOYMENT = "strong"


def _deployment(
    model_name: str, model: str, api_key: str | None, api_base: str | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"model": model}
    if api_key:
        params["api_key"] = api_key
    # Self-hosted providers (Ollama, vLLM, an OpenAI-compatible gateway) are not
    # reachable at a well-known host, so they need this and hosted vendors do not.
    # Omitted rather than passed as None: LiteLLM treats an explicit None as a
    # configured empty base for some providers, which fails later and further away.
    if api_base:
        params["api_base"] = api_base
    # Per-request ceiling. A generation that exceeds this is a hung provider, not
    # a slow one, and holding the student's connection open helps nobody.
    params["timeout"] = settings.model_timeout_seconds
    return {"model_name": model_name, "litellm_params": params}


def build_model_list() -> list[dict[str, Any]]:
    """One deployment per logical name.

    `strong`, `cheap` and `fallback` are logical names; which concrete provider
    serves each is deployment configuration. Swapping providers is an environment
    variable, never a code change (§8.4).

    **Each name appears at most once, and that is load-bearing** — see the module
    docstring. Two deployments sharing a name are load-balanced peers, not a
    priority chain, so naming the fallback `strong` handed it half the traffic.
    """
    models: list[dict[str, Any]] = []

    if settings.strong_model:
        models.append(
            _deployment(
                "strong", settings.strong_model, settings.model_api_key, settings.model_api_base
            )
        )
    if settings.cheap_model:
        models.append(
            _deployment(
                "cheap", settings.cheap_model, settings.model_api_key, settings.model_api_base
            )
        )

    # A different *provider*, which is what actually survives one vendor's outage
    # — not a second model from the same vendor (§8.2). Promoted to primary only
    # when no strong model is configured at all, so a fallback-only deployment
    # still answers instead of asking the Router for a name it does not have.
    if settings.fallback_model:
        name = "fallback" if settings.strong_model else "strong"
        models.append(
            _deployment(
                name,
                settings.fallback_model,
                settings.fallback_api_key,
                settings.fallback_api_base,
            )
        )
    return models


def build_fallback_chain(model_list: list[dict[str, Any]]) -> list[dict[str, list[str]]]:
    """What `strong` falls back to, in order, skipping what is not configured.

    A different vendor first: that is the outage this exists for. `cheap` is the
    same vendor as `strong`, so it shares the primary's outage and is a last
    resort rather than a failover — useful for a content-policy refusal or a
    model-specific fault, useless when the vendor is down.

    Referencing a deployment that was never registered is not harmless: the
    Router would route to a name that does not resolve, turning a recoverable
    provider error into an unrecoverable one.
    """
    names = {m["model_name"] for m in model_list}
    if PRIMARY_DEPLOYMENT not in names:
        return []
    chain = [n for n in ("fallback", "cheap") if n in names]
    return [{PRIMARY_DEPLOYMENT: chain}] if chain else []


def build_generation_fallback_chain(model_list: list[dict[str, Any]]) -> list[dict[str, list[str]]]:
    """What practice-question generation falls back to. **Never `cheap`.**

    The default chain is `strong -> fallback -> cheap`, which is right for chat:
    a degraded answer that cites correctly still helps, and the DEGRADED frame
    says so. Generation is different in two ways.

    First, `cheap` shares the primary's vendor, so it does not survive the outage
    a fallback exists for — it only helps on a model-specific fault. Second, and
    decisively, a generated practice question reaches a student with **no
    instructor gate**, and §9.0 permits that because the output is measured. The
    Feature B rubric scored the strong model. Quietly serving a weaker one means
    shipping unmeasured output under a measurement someone else earned.

    So generation gets the different-vendor fallback (announced by DEGRADED) and
    nothing else. With no fallback configured the chain is empty and a `strong`
    outage becomes UNAVAILABLE, which is the honest outcome.
    """
    names = {m["model_name"] for m in model_list}
    if PRIMARY_DEPLOYMENT not in names or "fallback" not in names:
        return []
    return [{PRIMARY_DEPLOYMENT: ["fallback"]}]


def deployment_of(response) -> str | None:
    """Which logical deployment produced this response or stream chunk.

    **Why not compare model names.** The previous check was
    `provider_used not in settings.strong_model` — a substring test between what
    the provider echoed back and what we configured. Providers return versioned
    ids, so `claude-opus-5-20260514` against a configured `anthropic/claude-opus-5`
    is not a substring, and every answer from the healthy primary would have been
    flagged DEGRADED. Nothing caught it because no hosted provider was ever
    exercised (LIMITATIONS §2).

    `_hidden_params["model_id"]` is the Router's own deployment id, so this asks
    the Router which of ITS deployments answered rather than inferring it from a
    string the vendor chose. Verified present on streamed chunks, not just on
    whole responses — the pipeline only ever sees chunks.

    Returns None when the id is absent (a stubbed router in a test, or a provider
    path that does not set it). Callers must treat None as "unknown", never as
    "degraded": inventing a degradation is the bug this replaced.
    """
    hidden = getattr(response, "_hidden_params", None) or {}
    model_id = hidden.get("model_id")
    if not model_id or _router is None:
        return None
    for deployment in _router.model_list:
        if (deployment.get("model_info") or {}).get("id") == model_id:
            return deployment.get("model_name")
    return None


_router = None


def get_router():
    """Lazily build the Router so the service starts without a provider.

    Deliberate: an unconfigured model must leave the platform and the API running
    and make *the tutor* unavailable, not crash the service on import.
    """
    global _router
    if _router is not None:
        return _router

    model_list = build_model_list()
    if not model_list:
        raise NoModelConfigured("No LLM provider configured")

    fallbacks = build_fallback_chain(model_list)

    from litellm import Router
    from litellm.types.router import RetryPolicy

    # Rule 2, across replicas. Cooldowns kept in process memory mean each
    # replica has to discover a dead provider for itself, so an outage costs
    # `allowed_fails` failures PER REPLICA instead of once for the deployment.
    # LiteLLM tracks cooldowns and tpm/rpm in Redis natively when given one, and
    # Redis is already Celery's broker here.
    redis_kwargs: dict[str, Any] = {}
    if settings.redis_url:
        from urllib.parse import urlparse

        parsed = urlparse(settings.redis_url)
        if parsed.hostname:
            redis_kwargs = {"redis_host": parsed.hostname, "redis_port": parsed.port or 6379}
            if parsed.password:
                redis_kwargs["redis_password"] = parsed.password
            log.info("litellm router: shared cooldowns via redis at %s", parsed.hostname)

    _router = Router(
        model_list=model_list,
        **redis_kwargs,
        # Rule 1: per-error-type retries. Auth errors are not retried — a wrong
        # key stays wrong, and retrying it just delays the honest failure.
        retry_policy=RetryPolicy(
            TimeoutErrorRetries=2,
            RateLimitErrorRetries=2,
            InternalServerErrorRetries=2,
            ContentPolicyViolationErrorRetries=0,
            AuthenticationErrorRetries=0,
        ),
        # Rule 2: cooldowns. After this many failures in a minute the deployment
        # leaves the pool rather than being retried into the ground.
        allowed_fails=3,
        cooldown_time=60,
        # Rule 1 again: a refusal is routed, not retried. Built from what is
        # actually registered — see build_fallback_chain.
        fallbacks=fallbacks,
        content_policy_fallbacks=fallbacks,
        num_retries=2,
        set_verbose=False,
    )
    log.info(
        "LiteLLM Router ready: %s", [m["model_name"] + "->" + m["litellm_params"]["model"] for m in model_list]
    )
    return _router


def reset_router() -> None:
    """Test hook: drop the cached Router so settings changes take effect."""
    global _router
    _router = None
