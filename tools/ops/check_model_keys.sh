#!/usr/bin/env bash
# Are the model credentials actually in effect? Four layers, because each one
# fails differently and only the last proves anything.
#
#   1. tutor config.yml   — what you edited
#   2. container env      — what the service actually got (differs until restart)
#   3. fingerprints match — catches "edited but never restarted", the classic
#   4. a real call        — the only check that proves the key is VALID
#
# Presence proves nothing: a key with a typo is present, is the right length, and
# fails every request. So this ends with one real completion per deployment and
# reports the error CLASS, which is what tells you which mistake you made:
#
#   AuthenticationError  -> key is wrong or revoked
#   APIConnectionError   -> base URL is wrong / provider unreachable
#   NotFoundError        -> model string is wrong for that provider
#   RateLimitError       -> key is VALID, you are just throttled (this is a pass)
#
# **No secret value is ever printed.** Values are reported as a length and a
# SHA-256 fingerprint (first 8 hex), which is enough to answer "is this the same
# string I pasted?" and useless to anyone reading over your shoulder.
#
#   bash tools/ops/check_model_keys.sh          # layers 1-3 only
#   bash tools/ops/check_model_keys.sh --call   # add the real calls (costs tokens)
set -u

CALL=0
[ "${1:-}" = "--call" ] && CALL=1

C=tutor_local-coursemate-1
ROOT=$(tutor config printroot 2>/dev/null)
CFG="$ROOT/config.yml"

fp() {  # length + fingerprint, never the value
  local v="${1:-}"
  if [ -z "$v" ]; then printf "empty"; return; fi
  printf "SET  len=%-4s fp=%s" "${#v}" "$(printf '%s' "$v" | sha256sum | cut -c1-8)"
}

from_cfg() {  # read one key out of config.yml without echoing it
  # Strips BOTH quote styles. YAML writes an empty string as '' and the first
  # version of this only stripped ", so a correctly-cleared value was reported
  # as the two-character string "''" and flagged as a mismatch against the
  # container's genuine empty. A verifier that cries wolf gets ignored.
  sed -n "s/^$1: *//p" "$CFG" 2>/dev/null | head -1 | sed "s/^[\"']//; s/[\"']$//"
}

from_env() {
  docker exec "$C" printenv "$1" 2>/dev/null || true
}

echo "=== 1+2. config.yml vs the running container ==="
printf "  %-32s %-34s %s\n" "VARIABLE" "config.yml" "container"
fail=0
# `config key|container variable`. They are NOT always the same name: tutor
# namespaces plugin settings, so the Ollama base is COURSEMATE_OLLAMA_API_BASE in
# config.yml and plain OLLAMA_API_BASE in the container (LiteLLM reads that name
# itself). Checking the container name on both sides read a config key that does
# not exist, reported "empty", and sent me setting the wrong variable — which
# then silently did nothing.
for pair in COURSEMATE_STRONG_MODEL COURSEMATE_MODEL_API_KEY COURSEMATE_MODEL_API_BASE \
            COURSEMATE_FALLBACK_MODEL COURSEMATE_FALLBACK_API_KEY COURSEMATE_FALLBACK_API_BASE \
            COURSEMATE_CHEAP_MODEL "COURSEMATE_OLLAMA_API_BASE|OLLAMA_API_BASE"; do
  v="${pair%%|*}"                       # config.yml key
  ev="${pair#*|}"; [ "$ev" = "$pair" ] && ev="$v"   # container variable
  c=$(from_cfg "$v"); e=$(from_env "$ev")
  case "$v" in
    *API_KEY) cs=$(fp "$c"); es=$(fp "$e") ;;
    *)        cs="${c:-empty}"; es="${e:-empty}" ;;
  esac
  mark=" "
  if [ "$c" != "$e" ]; then mark="!"; fail=1; fi
  label="$v"; [ "$ev" != "$v" ] && label="$v → $ev"
  printf " %s%-40s %-34s %s\n" "$mark" "$label" "$cs" "$es"
done

echo
if [ "$fail" -eq 1 ]; then
  echo "  ! = config.yml and the container DISAGREE."
  echo "    You edited the file but the service is still running the old value."
  echo "    Fix:  tutor config save && tutor local start -d coursemate"
else
  echo "  config.yml and the container agree."
fi

echo
echo "=== 3. what the Router will actually register ==="
docker exec "$C" python -c "
from coursemate_service.ai.client import build_model_list, build_fallback_chain
ml = build_model_list()
if not ml:
    print('  NONE — no provider configured')
for m in ml:
    p = m['litellm_params']
    print(f\"  {m['model_name']:9s} -> {p['model']:36s} key={'yes' if p.get('api_key') else 'no ':3s} base={p.get('api_base') or '(default)'}\")
print('  chat fallback chain:', build_fallback_chain(ml) or '(none)')
" 2>/dev/null

if [ "$CALL" -eq 0 ]; then
  echo
  echo "Layers 1-3 only. Presence is NOT validity — re-run with --call to prove"
  echo "each key actually works. That spends a few tokens per deployment."
  exit 0
fi

echo
echo "=== 4. one real call per deployment (the only proof) ==="
docker exec "$C" python -c "
import asyncio
from coursemate_service.ai.client import build_model_list, get_router

async def main():
    router = get_router()
    for m in build_model_list():
        name = m['model_name']
        try:
            r = await router.acompletion(
                model=name,
                messages=[{'role': 'user', 'content': 'Reply with the single word: ok'}],
                max_tokens=5,
                fallbacks=[],          # pinned: a failure must not be masked
            )
            text = (r.choices[0].message.content or '').strip()[:20]
            print(f'  {name:9s} PASS  answered {text!r}')
        except Exception as exc:
            cls = type(exc).__name__
            hint = {
                'AuthenticationError': 'key wrong or revoked',
                'APIConnectionError': 'base URL wrong / provider unreachable',
                'NotFoundError': 'model string wrong for this provider',
                'RateLimitError': 'key is VALID — throttled only',
            }.get(cls, '')
            verdict = 'PASS*' if cls == 'RateLimitError' else 'FAIL '
            print(f'  {name:9s} {verdict} {cls}{\"  (\" + hint + \")\" if hint else \"\"}')
            print(f'            {str(exc)[:150]}')

asyncio.run(main())
" 2>&1 | grep -vE "LiteLLM:WARNING|register_model|^$"

echo
echo "  PASS* = rate limited, which still proves the credential is accepted."
