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
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)


class NoModelConfigured(RuntimeError):
    """No provider is configured. The tutor is unavailable and says so."""


def _deployment(model_name: str, model: str, api_key: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"model": model}
    if api_key:
        params["api_key"] = api_key
    # Per-request ceiling. A generation that exceeds this is a hung provider, not
    # a slow one, and holding the student's connection open helps nobody.
    params["timeout"] = settings.model_timeout_seconds
    return {"model_name": model_name, "litellm_params": params}


def build_model_list() -> list[dict[str, Any]]:
    """Deployments in priority order.

    `strong` and `cheap` are logical names the pipeline asks for; which concrete
    provider serves them is deployment configuration. Swapping providers is an
    environment variable, never a code change (§8.4).
    """
    models: list[dict[str, Any]] = []

    if settings.strong_model:
        models.append(_deployment("strong", settings.strong_model, settings.model_api_key))
    if settings.cheap_model:
        models.append(_deployment("cheap", settings.cheap_model, settings.model_api_key))

    # A different *provider*, which is what actually survives one vendor's outage
    # — not a second model from the same vendor (§8.2).
    if settings.fallback_model:
        models.append(
            _deployment("strong", settings.fallback_model, settings.fallback_api_key)
        )
    return models


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

    from litellm import Router
    from litellm.types.router import RetryPolicy

    _router = Router(
        model_list=model_list,
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
        # Rule 1 again: a refusal is routed, not retried.
        fallbacks=[{"strong": ["cheap"]}],
        content_policy_fallbacks=[{"strong": ["cheap"]}],
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
