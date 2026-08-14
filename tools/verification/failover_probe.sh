#!/usr/bin/env bash
# Does the cross-vendor fallback chain actually work on the running service?
#
#   MSYS_NO_PATHCONV=1 tools/verification/failover_probe.sh
#
# The chain, the DEGRADED frame, `deployment_of()` and `provider_failures_total`
# have all existed since v1 and **none of them had ever fired against a real
# outage** — there was only ever one provider, so there was nothing to fail over
# to. Every test of this path drove a scripted router. This drives the real one.
#
# **Nothing in the deployment is modified.** The probe runs inside the service
# container and builds its OWN Router from the deployed settings with exactly one
# field overridden, then calls `reset_router()` to restore. The container's
# config, the tutor config and the running uvicorn workers are untouched, so
# "restore" is guaranteed by the process exiting rather than by a cleanup step
# that might not run. Same reasoning as claim_verify_live.sh, which drives the
# pipeline directly because a real HTTP request would test authentication.
#
# **What this can and cannot show about metrics.** `provider_failures_total` is
# incremented in `pipeline.py` only when an exception reaches it — and the Router
# swallows a provider failure whenever a fallback succeeds. So a SUCCESSFUL
# degradation does not move the counter, by construction. That is measured here
# rather than asserted away, and it is a real observability gap: a primary that
# is silently degrading every request is invisible in metrics.
set -eu

CM=tutor_local-coursemate-1
OFFERING="course-v1:OpenedX+DemoX+DemoCourse"

echo "=== 0. preflight: what is deployed right now ==="
docker exec "$CM" python -c "
from coursemate_service.ai.client import build_model_list, build_fallback_chain
ml = build_model_list()
for m in ml:
    print(f\"  {m['model_name']:9s} -> {m['litellm_params']['model']}\")
print('  chat chain:', build_fallback_chain(ml) or '(none)')
"

docker exec -i "$CM" python - "$OFFERING" <<'PY'
"""Baseline -> primary down -> everything down -> restored, on the real pipeline."""
import asyncio
import sys
import time

from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, FrameType

from coursemate_service import metrics
from coursemate_service.ai import client
from coursemate_service.ai.pipeline import AnswerPipeline
from coursemate_service.ai.retrieval import CourseContextProvider

OFFERING = sys.argv[1]
QUESTION = "What are video transcripts used for?"

failures = []


def claims():
    """A REAL enrolled identity, not an invented one.

    The first version used `username="probe"`, and every step abstained before
    the model was ever reached: the boundary re-derives enrollment on each call
    and fails closed, so an unknown user retrieves nothing and the confidence
    gate abstains. Correct behaviour, useless probe — it measured authorization
    while claiming to measure failover. `admin` is the identity the eval harness
    uses and the one the authorization matrix records as allowed.
    """
    now = int(time.time())
    return StudentClaims(
        sub="1", username="admin", course_id=OFFERING, offering_id=OFFERING,
        roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 900, iat=now,
        usage_key="block-v1:probe", block_id="probe",
    )


async def ask():
    """One real generation. Returns what the student would have seen."""
    pipeline = AnswerPipeline(CourseContextProvider())
    out = {"text": "", "citations": [], "degraded": None, "error": None, "frames": []}
    async for f in pipeline.stream(ChatRequest(question=QUESTION), claims()):
        out["frames"].append(f.type)
        if f.type == FrameType.TOKEN:
            out["text"] += f.text or ""
        elif f.type == FrameType.CITATION:
            out["citations"].append(f.citation.display_name or f.citation.usage_key)
        elif f.type == FrameType.DEGRADED:
            out["degraded"] = f.provider
        elif f.type == FrameType.ERROR:
            out["error"] = f.error_code
    return out


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def run(step):
    client.reset_router()
    before = metrics.snapshot().get("provider_failures_total", 0)
    t0 = time.perf_counter()
    res = asyncio.run(ask())
    res["ms"] = (time.perf_counter() - t0) * 1000
    res["failures_delta"] = metrics.snapshot().get("provider_failures_total", 0) - before
    print(f"\n--- {step} ---")
    print(f"  answered {len(res['text'])} chars in {res['ms']:.0f} ms")
    print(f"  citations: {res['citations'] or '(none)'}")
    print(f"  degraded : {res['degraded'] or '(no)'}")
    print(f"  error    : {res['error'] or '(none)'}")
    print(f"  provider_failures_total delta: {res['failures_delta']}")
    return res


REAL_KEY = client.settings.model_api_key
REAL_BASE = client.settings.model_api_base

# **The response cache has to be off for this probe to test anything.**
# The same student asking the same question as a first turn is a cache HIT, so
# after the baseline populated it every later step was served in ~15 ms from
# storage with the identical 255-char answer and three citations — while the
# provider was unreachable. Breaking the provider changed nothing because no
# provider was ever called. That is the cache working exactly as designed and it
# makes a failover probe measure the cache instead.
print(f"  response cache was: {client.settings.response_cache_enabled} — disabling for this probe")
client.settings.response_cache_enabled = False

# --- 0. warm the authorization path --------------------------------------
# The first OAuth token exchange against a cold LMS has been measured at 5.58 s
# against a 5 s `authz_timeout_seconds`, so it times out, the boundary fails
# closed, retrieval returns nothing and the gate abstains. That reads exactly
# like a model failure and is not one — two hours of Phase D2 went into it once
# already. Two throwaway retrievals; the third is cached and free.
from coursemate_service.boundary.impl import boundary  # noqa: E402

for attempt in range(2):
    try:
        hits = boundary.retrieve_course_context(QUESTION, OFFERING, claims(), limit=3)
        print(f"  authz warm-up {attempt + 1}: {len(hits)} chunk(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"  authz warm-up {attempt + 1}: {type(exc).__name__}: {exc}")

# --- 1. baseline ----------------------------------------------------------
r = run("1. BASELINE (primary healthy)")
check("baseline answers", len(r["text"]) > 0, f"{len(r['text'])} chars")
check("baseline cites", len(r["citations"]) > 0, f"{len(r['citations'])} citation(s)")
check("baseline is NOT degraded", r["degraded"] is None)
check("baseline has no error", r["error"] is None)

# --- 2. primary down ------------------------------------------------------
# An invalid key rather than a dead base: `_deployment` hands
# `settings.model_api_base` to BOTH strong and cheap, so breaking the base would
# take the local floor down with the primary and prove nothing about failover.
# Ollama ignores the key, so this isolates the hosted provider exactly.
client.settings.model_api_key = "sk-or-v1-invalid-key-for-failover-probe"
r2 = run("2. PRIMARY DOWN (hosted key invalidated)")
check("the tutor still answers", len(r2["text"]) > 0, f"{len(r2['text'])} chars")
check("the answer is still cited", len(r2["citations"]) > 0,
      f"{len(r2['citations'])} citation(s)")
check("DEGRADED is emitted", r2["degraded"] is not None, str(r2["degraded"]))
check("no error frame — the student is served", r2["error"] is None)
check("a successful fallback does NOT move provider_failures_total",
      r2["failures_delta"] == 0,
      "counter only fires when the WHOLE chain fails; see the header")

# --- 3. everything down ---------------------------------------------------
# Both deployments unreachable. With no `fallback` configured there is exactly
# one hosted provider, so this IS the "both hosted providers down" case, and it
# additionally removes the local floor.
client.settings.model_api_base = "http://127.0.0.1:1"
r3 = run("3. EVERYTHING DOWN (hosted + local floor)")
check("the tutor refuses rather than fabricating", r3["error"] is not None,
      str(r3["error"]))
check("nothing was answered", len(r3["text"]) == 0)
check("provider_failures_total DID increase", r3["failures_delta"] >= 1,
      f"delta={r3['failures_delta']}")

# --- 4. restored ----------------------------------------------------------
client.settings.model_api_key = REAL_KEY
client.settings.model_api_base = REAL_BASE
r4 = run("4. RESTORED")
check("the primary answers again", len(r4["text"]) > 0, f"{len(r4['text'])} chars")
check("no longer degraded", r4["degraded"] is None)
check("cited again", len(r4["citations"]) > 0)

client.reset_router()

print()
if failures:
    print(f"VERDICT: FAIL — {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("VERDICT: PASS — the chain failed over, degraded visibly, and recovered")
PY

echo
echo "=== 5. the deployment is untouched ==="
docker exec "$CM" python -c "
from coursemate_service.ai.client import build_model_list
for m in build_model_list():
    p = m['litellm_params']
    print(f\"  {m['model_name']:9s} -> {p['model']:46s} key={'yes' if p.get('api_key') else 'no'}\")
"
curl -s -o /dev/null -w "  /coursemate/health       %{http_code}\n" --max-time 10 http://local.openedx.io/coursemate/health || true
curl -s -o /dev/null -w "  /coursemate/health/ready %{http_code}\n" --max-time 10 http://local.openedx.io/coursemate/health/ready || true
