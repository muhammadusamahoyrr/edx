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
    #:
    #: **Know which scale this is on before tuning it.** It is compared against
    #: the BLENDED rerank score, not the raw query-term coverage that
    #: `knowledge/store.py` computes and documents. `LexicalReranker` overwrites
    #: coverage with 0.60·coverage + 0.15·proximity + 0.25·title, and the gate
    #: reads that. Measured over the 28-question gold set against the live index:
    #:
    #:     blended ≈ 0.855 × coverage on average  (min 0.675, lower in 19 of 28,
    #:     higher in 1 where a title match lifted it)
    #:     blended  = 0.600 × coverage  when proximity and title are both zero
    #:
    #: So 0.35 here is a coverage bar of roughly 0.41 typically, and 0.583 in the
    #: worst case — NOT "35% of the question's words appeared". Tuning this
    #: against logged abstentions, which the note above invites, means tuning
    #: against the blend. With `rerank_enabled=False` it is raw coverage again.
    #:
    #: 0.35 measured as the optimum on that gold set: at 0.30 a false answer
    #: appears, at 0.40 a correct answer is lost. n=28, one course, one rater —
    #: indicative, and the interval above is still the honest caveat.
    confidence_threshold: float = 0.35
    #
    # `abstain_on_tie` used to sit here, defaulting True with a comment saying
    # "we tune toward abstention". It was read by nothing, and its stated default
    # was the opposite of the implemented rule: `ai/gate.py` compares
    # `top_score < threshold`, so a score exactly AT tau answers. A setting that
    # contradicts the code is worse than no setting — it is a claim a reader
    # believes. The tie rule now lives in `gate.evaluate`, next to the comparison
    # that decides it. Removed 2026-08-13; behaviour unchanged.

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
    #: `strong`'s credential. **Not `cheap`'s** — see `cheap_api_key` below.
    model_api_key: str | None = None
    #: Only needed by providers that are not reachable at a well-known host —
    #: Ollama, vLLM, an OpenAI-compatible gateway. `None` leaves LiteLLM's own
    #: default, which is right for every hosted vendor.
    model_api_base: str | None = None

    #: **`cheap` gets its own credential and base URL (2026-08-14).** Both tiers
    #: used to be handed `model_api_key` and `model_api_base`, which was fine
    #: while they were one vendor and became a trap the moment ADR-0001 made
    #: `strong` hosted (OpenRouter) and `cheap` local (Ollama).
    #:
    #: The trap is specific, and the docs walked straight into it. This file and
    #: `deploy/tutor-plugin/coursemate.yml` both presented `MODEL_API_BASE` as
    #: *the* provider-agnostic way to point at a local model. Following that
    #: advice with the current topology hands the OpenRouter deployment the
    #: Ollama URL as well, and the primary starts failing for a reason nothing
    #: in the config names. The live deployment only avoided it by leaving
    #: `MODEL_API_BASE` empty and relying on LiteLLM reading `OLLAMA_API_BASE`
    #: itself — a provider-specific side channel that works and is not what
    #: either document describes.
    #:
    #: The second half is smaller and still wrong: `cheap` was being sent the
    #: HOSTED vendor's key. Harmless while Ollama is on the same host; a
    #: credential leak the day it is not.
    #:
    #: `None` on either falls back to the `model_*` value, so a single-vendor
    #: deployment configures one pair and changes nothing.
    cheap_api_key: str | None = None
    cheap_api_base: str | None = None

    #: A different *provider*, which is what survives one vendor's outage — not a
    #: second model from the same vendor.
    #:
    #: **This is where the self-hosted model goes.** §8.4 deferred it on the
    #: grounds that a simultaneous outage of both hosted providers should make the
    #: tutor unavailable rather than silently worse. That argument was about
    #: *quality*, and it still holds — which is why falling back is announced by a
    #: DEGRADED frame rather than hidden. What changed is that an Ollama host is
    #: already running here for embeddings, so the second provider costs one
    #: environment variable instead of a new service:
    #:
    #:     COURSEMATE_FALLBACK_MODEL=ollama/qwen2.5:7b
    #:     COURSEMATE_FALLBACK_API_BASE=http://host.docker.internal:11434
    #:
    #: **The old reason for setting this is no longer the reason (2026-08-14).**
    #: This said: *"Leave it unset and the chain is `strong -> cheap` only, which
    #: shares the primary's vendor and therefore its outage."* ADR-0001 retracted
    #: that sentence — `strong` is hosted and `cheap` is local, so they share
    #: neither vendor, machine, nor failure mode, and `cheap` is in fact the
    #: deployment most likely to survive a hosted outage. The correction landed in
    #: `ai/client.py` and in the ADR and was missed here.
    #:
    #: The gap that remains is narrower and still real: with this unset there is
    #: no second HOSTED vendor, and practice generation has no fallback at all —
    #: `build_generation_fallback_chain` will not use `cheap`, deliberately
    #: (ADR-0001 decision 4). LIMITATIONS §2 carries it.
    fallback_model: str | None = None
    fallback_api_key: str | None = None
    fallback_api_base: str | None = None

    #: Embedding model for the generator's semantic duplicate check (§9.0).
    #:
    #: Routed through LiteLLM like every other model, so it needs no second
    #: provider path: `ollama/*` resolves via the same `OLLAMA_API_BASE` the
    #: `cheap` tier already uses. Measured at ~300–500 ms per call against a
    #: generation that takes seconds, so it is noise on the serve path.
    #:
    #: **Empty disables the check**, and the generator then relies on the token
    #: -overlap reprint check alone. That is the correct behaviour for a
    #: deployment with no embedding provider, and it is what the test suite
    #: uses — a unit test that reaches the network is not a unit test.
    duplicate_embedding_model: str = "ollama/nomic-embed-text"

    #: The duplicate check's own ceiling. **Deliberately NOT
    #: `model_timeout_seconds`**, which is sized for a generation and is 300 in
    #: this deployment — borrowing it would let an optional half-second check
    #: hold a student's connection for five minutes per attempt, ten across the
    #: retry.
    #:
    #: Measured at 490–623 ms for a batch of four texts against local Ollama, so
    #: 5 s is roughly 8× headroom. A slower provider is a hung one as far as this
    #: check is concerned, and giving up costs nothing: the timeout is caught,
    #: the check returns "no opinion", and the token-overlap check still runs.
    #:
    #: A *reachable but wedged* provider is the case this exists for. Ollama
    #: dying outright fails fast; the `socat` forwarder listening while Ollama is
    #: unusable is a documented failure of this stack, and there the connection
    #: opens and never answers.
    semantic_embedding_timeout_seconds: float = 5.0

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

    # `reranker_model` was declared here and read by nothing: the reranker is
    # lexical, so a model name was a setting for a component that does not
    # exist. The target it named is preserved in `knowledge/rerank.py`, beside
    # the reasoning for shipping the lexical one first. Removed 2026-08-13.

    #: Retrieval index. A file, not a service: at course scale SQLite FTS5 is
    #: faster than a network hop to a vector database would be.
    index_path: str = "/data/coursemate-index.db"

    #: Past-paper questions (§7.6). A separate file from the index on purpose: a
    #: course reindex rewrites that one wholesale, and it must not be able to take
    #: a term's worth of extracted papers with it.
    examprep_path: str = "/data/coursemate-examprep.db"

    # --- the exam-prep agent (§6.5, §7) --------------------------------------
    #: **Ships dark.** The kill switch is read at the API layer, not inside the
    #: agent, so `False` means the deterministic path is reached and no agent code
    #: runs at all — rather than an agent that starts and then declines.
    #:
    #: Default `False` is the inverse of the `require_grounding` lesson and the
    #: same principle. A *safety control* that must be switched on is not a
    #: control; a *new subsystem* that must be switched on is a subsystem nobody
    #: enables by accident. Which default is right depends on which way the
    #: failure runs, and here an unproven agent loop answering students is the
    #: failure.
    agent_enabled: bool = False

    #: Answer evaluation (F2). **Off, and it must stay off until the accuracy of
    #: the comparison has been measured.**
    #:
    #: §11.2 settles that on the personal path "measurement *is* the control —
    #: there is no human backstop". There is currently no grading dataset and no
    #: grading rubric — `feature_b_rubric.py` scores generated QUESTIONS — so
    #: nothing has established how often this comparison is right. Turning this
    #: on ships an unmeasured claim to a student.
    #:
    #: Enabling it changes one more thing worth stating out loud: the student's
    #: written answer leaves the page for the first time. Until F2 the UI said it
    #: never would.
    #:
    #: Even ON, a question with no `reference_answer` abstains — which today is
    #: every question in the live bank.
    answer_evaluation_enabled: bool = False

    #: Loop ceiling. Six is enough for the deepest planned path — CLOs, mastery,
    #: past questions, course content, one re-plan after an error, synthesis — and
    #: a cap that is routinely hit is a budget, not a safety net, so hitting it is
    #: logged as a defect rather than absorbed.
    agent_max_iterations: int = 6
    #: Wall clock across the whole loop, distinct from `model_timeout_seconds`
    #: which bounds one provider call. Without this, six calls that each take 55s
    #: is a five-minute request that never technically timed out.
    agent_timeout_seconds: float = 30.0

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
    #: Practice streams one student may hold open at once. A different question
    #: from the rate above: that one caps how often a stream is *started*, this
    #: caps how many are *running*, and only the second bounds how much of the
    #: provider's concurrency one student can occupy while everyone else waits.
    #:
    #: Two, not one: a student who opens a second tab, or reloads before the
    #: first stream has finished unwinding, is doing something ordinary. Two
    #: absorbs that; one would reject it.
    max_concurrent_streams: int = 2
    #: Tokens one student may spend on chat answers in one course in one UTC day.
    #: The third abuse question, and the only one denominated in money: the two
    #: limits above bound how OFTEN and how MANY AT ONCE, and twenty questions a
    #: minute all day is within both.
    #:
    #: **Measured on the live stack 2026-08-12**, replacing the estimate this
    #: comment shipped with (which said ~2,200 prompt tokens and 30–35 answers —
    #: both wrong, and wrong in the direction that undersold the ceiling).
    #: Across seven real generations: 660–785 tokens for a first turn, ~908 for a
    #: short conversation, 1,295 for a nine-turn one — the prompt grows with the
    #: history the browser carries. So 100,000 is roughly **75–150 answers** per
    #: student per course per day. Production runs `max_output_tokens=400`.
    #:
    #: At hosted Sonnet-class pricing that is under $0.50 per student per course
    #: per day worst case. On this deployment the provider is local, so the
    #: ceiling bounds compute rather than money. See BENCHMARKS §3.9.
    #:
    #: Counted in tokens, not dollars, because a price table is wrong the moment
    #: a provider reprices or the router falls back to another deployment.
    #: Zero or less disables the ceiling — a mis-set limit must not take the
    #: tutor offline.
    student_daily_token_budget: int = 100_000

    #: First-turn response cache (§6.4). A kill switch rather than a permanent
    #: flag: §10.2 calls response caching the place isolation quietly fails after
    #: every filter is written correctly, so there has to be a way to turn it off
    #: that does not need a deploy to reason about.
    response_cache_enabled: bool = True
    #: TTL ceiling. The index version in the key already invalidates on reindex,
    #: so this is not the correctness mechanism — it bounds how long an answer
    #: can outlive a change the version did not capture (a prompt edit, a model
    #: swap, a tau change). One hour is short enough that such a change is not
    #: worth a manual purge and long enough to absorb a lecture-hall spike of the
    #: same question.
    response_cache_ttl_seconds: int = 3600

    # `per_course_ingest_ceiling_usd` was declared here, under this same
    # "abuse and cost" heading, and enforced by nothing — no ingest path ever
    # read it. Sitting beside two limits that ARE enforced, it read as a third
    # control rather than as an intention, which is the misleading direction.
    # Ingest spend is currently unbounded; LIMITATIONS says so. Removed
    # 2026-08-13 rather than left as a number that protects nothing.

    # --- feature flags for designed-but-dormant work --------------------------
    lexical_retrieval_enabled: bool = False  # Meilisearch half of hybrid (§6.1)
    aside_enabled: bool = False  # XBlockAside, vertical-scoped (§3.1)

    contract_version_lock: bool = True


settings = Settings()  # type: ignore[call-arg]
