"""Service configuration.

Every credential lives here and nowhere in the platform package (§10.4): the
XBlock mints a token and renders a UI, so it has no reason to know a provider key.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COURSEMATE_", env_file=".env")

    # --- tenancy --------------------------------------------------------------
    #: Single-valued in the MVP (§3.5). Present from day one because retrofitting
    #: an isolation key later is expensive and carrying an unused one is free.
    tenant: str = "default"

    # --- the student hop (§3.4) ----------------------------------------------
    jwt_signing_key: str = Field(min_length=32)
    service_credential: str = Field(min_length=32)

    # --- grounding (§8.5) -----------------------------------------------------
    #: Initialized from the pilot, refined from logged production abstentions. A
    #: 20-30 question pilot yields ~15 negatives, which cannot calibrate this to
    #: any useful precision — so it ships as a starting point with a confidence
    #: interval, not as a settled number.
    confidence_threshold: float = 0.35
    #: We tune toward abstention: a confidently wrong answer costs a student more
    #: than an unnecessary "not covered".
    abstain_on_tie: bool = True

    # --- claim verification (§8.5) -------------------------------------------
    #: Emit UNSUPPORTED_CLAIM for sentences whose content words are largely
    #: absent from the retrieved material. On by default: the frame has been in
    #: the contract and rendered by the browser since v1 with nothing sending it,
    #: and a check that ships switched off is the same problem as a default that
    #: ships switched off.
    verify_claims: bool = True
    #: Fraction of a sentence's content words that must appear in the retrieved
    #: material. 0.4 is a starting point, not a calibrated number — like the
    #: confidence threshold it wants tuning against logged production output, and
    #: it errs toward marking too much, because an over-cautious tutor is
    #: recoverable and a confidently unsupported one is not.
    claim_support_threshold: float = 0.4

    # --- retrieval ------------------------------------------------------------
    retrieve_candidates: int = 20
    rerank_top_k: int = 5
    #: Under load or on reranker failure: skip and take top-k by merged score.
    #: Measurably worse, explicitly logged, never an outage (§8.2).
    rerank_enabled: bool = True

    # --- models (§8.2, §8.4) --------------------------------------------------
    #: Provider strings in LiteLLM form, e.g. "anthropic/claude-opus-5",
    #: "openai/gpt-4o", "gemini/gemini-2.0-flash", "ollama/llama3".
    #: Swapping providers is configuration, never a code change.
    strong_model: str = "anthropic/claude-opus-5"
    cheap_model: str = "anthropic/claude-haiku-4-5-20251001"
    model_api_key: str | None = None

    #: A different *provider*, which is what survives one vendor's outage — not a
    #: second model from the same vendor. The self-hosted local model stays
    #: deferred (§8.4): a simultaneous outage of both hosted providers means the
    #: tutor is unavailable and says so, rather than degrading silently.
    fallback_model: str | None = None
    fallback_api_key: str | None = None

    #: Per-request ceiling. Beyond this the provider is hung, not slow, and
    #: holding the student's connection open helps nobody.
    model_timeout_seconds: int = 60
    max_output_tokens: int = 800

    #: Development only. When set, LiteLLM returns this instead of calling a
    #: provider — exercising the real Router, retry policy, fallback config and
    #: streaming path with no network call and no key. Never set in production.
    mock_response: str | None = None

    #: When true the tutor abstains unless retrieval supplied context (§8.5).
    #:
    #: **Defaults True since retrieval landed.** It was False through Phase 5,
    #: when there was no retriever and every question would have abstained — a
    #: correct answer to the wrong question, and nothing demonstrable. That
    #: reason expired with Phase 6 and the default did not follow it.
    #:
    #: Leaving it False meant every abstention behaviour — the confidence gate,
    #: `ABSTAINED`, `PREPARING` — sat behind a flag a fresh install had OFF. A
    #: safety control that must be switched on is not a control; it is a setting
    #: someone will forget. An unindexed course now says "still being prepared"
    #: out of the box rather than answering from the model's own knowledge.
    require_grounding: bool = True

    reranker_model: str = "BAAI/bge-reranker-base"

    #: Retrieval index. A file, not a service: at course scale SQLite FTS5 is
    #: faster than a network hop to a vector database would be.
    index_path: str = "/data/coursemate-index.db"

    # --- authorization re-derivation (§10.1) ---------------------------------
    #: Open edX owns enrollment; we never keep our own list.
    lms_url: str = "http://lms:8000"
    #: OAuth2 client-credentials for the CourseMate service account. The legacy
    #: X-Edx-Api-Key header is gone in current Open edX and returns 401.
    lms_client_id: str = ""
    lms_client_secret: str = ""
    #: Short by design (§6.4): a REVOKED enrollment must stop working quickly.
    #: Long enough that the common case costs nothing.
    authz_cache_ttl_seconds: int = 60
    authz_timeout_seconds: float = 5.0
    #: Fails closed when the platform is unreachable. Set False only for local
    #: development against a stubbed LMS -- never in a deployment serving real
    #: course content.
    enforce_enrollment: bool = True

    # --- shared state across replicas (§3 of LIMITATIONS) ---------------------
    #: Redis is already in every Tutor deployment — it is Celery's broker — so
    #: this adds no infrastructure. Empty means "single process": the rate
    #: limiter and authz cache fall back to in-memory, which is correct for one
    #: replica and silently wrong for two.
    #:
    #: What this fixes, and it is three separate bugs with one cause:
    #:   * the rate limiter allowed N x the limit with N replicas
    #:   * invalidating an entitlement cleared one replica's cache
    #:   * LiteLLM cooldowns were per-process, so a dead provider was
    #:     rediscovered independently by every replica
    redis_url: str = ""

    # --- abuse and cost (§10.8) ----------------------------------------------
    #: These live at the boundary alongside authorization so a new agent node
    #: cannot bypass them. Since v8 they also cover student traffic, which used
    #: to be rate-limited in the XBlock.
    student_requests_per_minute: int = 20
    per_course_ingest_ceiling_usd: float = 5.0

    # --- feature flags for designed-but-dormant work --------------------------
    lexical_retrieval_enabled: bool = False  # Meilisearch half of hybrid (§6.1)
    aside_enabled: bool = False  # XBlockAside, vertical-scoped (§3.1)

    contract_version_lock: bool = True


settings = Settings()  # type: ignore[call-arg]
