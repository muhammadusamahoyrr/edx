# ADR-0001 — Three-tier model routing, and why the local floor may not generate

**Status:** Accepted
**Date:** 2026-08-14
**Supersedes:** nothing. This is the first ADR in the repo.

---

## Context

Until 2026-08-14 CourseMate ran on **one provider and one model**. Both logical
tiers pointed at the same local deployment:

    COURSEMATE_STRONG_MODEL = ollama_chat/qwen2.5:7b
    COURSEMATE_CHEAP_MODEL  = ollama_chat/qwen2.5:7b
    COURSEMATE_FALLBACK_MODEL = (empty)

Everything the design says about failover — the priority chain, the `DEGRADED`
frame, `deployment_of()`, `provider_failures_total` — existed, was unit-tested
against a scripted router, and **had never executed against a real outage**.
There was nothing to fail over to. `LIMITATIONS.md §2` recorded this honestly;
it was still a claim standing on nothing.

A proposal on the table was to branch the primary across two hosted providers in
parallel, then chain onward. That is rejected below.

## Decision

**Three logical deployments, one vendor each, in a strictly linear chain.**

| slot | serves | current value |
|---|---|---|
| `strong` | every student request, and all generation | `openrouter/meta-llama/llama-3.3-70b-instruct` |
| `fallback` | cross-vendor failover | *(unset — see Consequences)* |
| `cheap` | the CLO tagger, and the last-resort floor for chat | `ollama_chat/qwen2.5:7b` |

    chat        strong → fallback → cheap
    generation  strong → fallback            (never cheap)

Four sub-decisions, each of which was tempting to take the other way.

### 1. No load-balanced head

Two deployments sharing a `model_name` are **load-balanced peers, not a priority
chain**. This repo has already shipped that bug: measured against litellm 1.94.1
with both providers healthy, **20 of 40 calls went to the "fallback"**.

Branching `strong` across two vendors would reintroduce it deliberately, and the
cost is not just tidiness:

* **Reproducibility.** The benchmark discipline rests on deterministic retrieval
  so that a change is attributable rather than lost in sampling noise. A
  coin-flipped answering model destroys that.
* **§9.0.** A generated practice question reaches a student ungated *because the
  output is measured*. If the answering model varies per call, "measured" stops
  meaning anything.

Load-balancing to spread free-tier quota is a legitimate feature. It is a
different feature from failover and must not be built by conflating the two.

### 2. `cheap` means "cheapest deployment that can still answer"

One name, one meaning, two jobs: the batch tier for `clo_tagger.py`
(`TAGGING_DEPLOYMENT = "cheap"`) and the floor for chat. Tagging is offline,
batch, re-runnable and has no student waiting, so a slow local model there costs
nothing. Keeping them as one slot avoids a fourth name whose only purpose is to
be almost the same as an existing one.

### 3. Local last — but the ordering is configuration, not policy

Quality says local last: `qwen2.5:7b` on CPU is ~25 s cold and times out on nine
of ten agent planning calls. Privacy says local first, or local only.

Both are right for different operators, so the chain is built from environment
variables and an institution can invert it without touching code. **The default
is quality-ordered; the offline-only deployment is a supported configuration, not
a fork.** That is a documented feature — see the data-flow note in
`LIMITATIONS.md`.

### 4. Generation never falls back to `cheap`

This is the decision most likely to be "tidied up" by someone later, so the
reasoning is recorded in full.

§9.0 permits a generated practice question to reach a student **with no
instructor gate** because the output is measured. The Feature B rubric scored the
strong model. Anything else answering means shipping unmeasured output under a
measurement someone else earned — the protection would still be claimed in the
design while no longer being provided.

**The old justification for this rule is now false, and its replacement is
weaker.** `build_generation_fallback_chain` used to open with *"`cheap` shares
the primary's vendor, so it does not survive the outage a fallback exists for"*.
True while both tiers were one hosted vendor. False from the moment `strong`
moved to OpenRouter and `cheap` became local: they now share neither vendor, nor
machine, nor failure mode.

The argument has in fact **inverted**. On availability grounds `cheap` is now the
*best* failover in the list — the only deployment that survives every hosted
outage at once. Excluding it is therefore a deliberate trade of availability for
measurement, not the free choice it used to look like.

With no `fallback` configured the generation chain is **empty**, so a `strong`
outage makes practice generation UNAVAILABLE while chat degrades to local. That
asymmetry is intentional: no question at all is better than an unmeasured one
presented as measured.

**If the local model is ever scored by the Feature B rubric, revisit this on the
evidence rather than leaving it standing on inertia.**

## Evidence

### Same context, pinned deployments (2026-08-14)

`eval/run_model_comparison.py`, 2 questions, identical retrieved context per
question, each call pinned by deployment name with `fallbacks=[]`.

| deployment | answered | median total | median first token | median chars | unsupported |
|---|---|---|---|---|---|
| `strong` — OpenRouter llama-3.3-70b | 2/2 | **4,316 ms** | 3,512 ms | 335 | **0** |
| `cheap` — local qwen2.5:7b | 2/2 | **49,120 ms** | 1,039 ms | 811 | 1 |

The hosted model is ~11× faster end to end, half as verbose, and stayed inside
its sources on both questions. The local model *starts* faster — 1,039 ms to
first token against 3,512 ms — and then generates slowly, which for a streaming
UI is not nothing.

### Failover, against a real outage (2026-08-14)

`tools/verification/failover_probe.sh`. **The first time this chain has ever
fired for real.**

| step | result |
|---|---|
| baseline | 255 chars, 3 citations, not degraded, 7,463 ms |
| primary key invalidated | 334 chars, **3 citations still**, `DEGRADED: qwen2.5:7b`, no error, 27,084 ms |
| hosted **and** local unreachable | 0 chars, `ERROR: unavailable`, `provider_failures_total +1` |
| restored | 255 chars, 3 citations, not degraded, 3,752 ms |

The middle row is the decision working: the hosted vendor was gone, the student
still received a cited answer from course material, and the UI was told the
answer was degraded.

## Consequences

**Accepted:**

* Two providers are active, not three. `fallback` is unset, so the live chain is
  `strong → cheap`: hosted primary, local floor, no second hosted vendor. This
  satisfies "more than one provider" and does **not** yet give cross-vendor
  failover *between hosted vendors*. Generation currently has no fallback at all.
* Student questions leave the machine in normal operation. The offline-only
  configuration remains supported and documented.
* A hosted provider means a key, a ToS and a rate limit per vendor. Adding a
  third is deferred until the first two have been observed failing.
* `allowed_fails=3, cooldown_time=60` are untuned against real free-tier
  behaviour. Expect to revisit once there is traffic.

**Discovered, and not fixed by this ADR:**

`provider_failures_total` **cannot increase on a successful failover.** It is
incremented in `pipeline.py` only when an exception reaches the pipeline, and the
Router swallows a provider failure whenever a fallback succeeds. Measured in
Phase 4: the degraded step moved the counter by **0**; only the total-outage step
moved it by 1.

So a primary that is silently degrading *every* request is invisible in metrics,
while the name suggests otherwise. The counter measures "generations that failed
entirely". Recorded in `LIMITATIONS.md`; a `degraded_answers_total` counter would
close it, and is deliberately not added here because this ADR changes no
behaviour.

## Alternatives rejected

| alternative | why not |
|---|---|
| Parallel hosted head (`strong` = two vendors) | load-balances rather than chains; destroys reproducibility and the §9.0 measurement argument |
| Four-level chain (A → B → C → local) | `build_model_list` registers exactly three names; needs `fallback_model` to become a list. Deferred until two vendors have proven insufficient |
| OpenRouter as `fallback` behind Groq | OpenRouter is a **gateway**, not a vendor, and routes to Groq among others — the two hops could resolve to the same upstream and fail together |
| Let `cheap` serve generation | §9.0. See Decision 4 |
