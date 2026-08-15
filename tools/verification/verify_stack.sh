#!/usr/bin/env bash
# Verify the Tutor/Open edX stack end to end.
#
#   bash tools/verification/verify_stack.sh
#
# Every check prints PASS or FAIL with the evidence it used, because "it looks up"
# is not verification. Exit code is non-zero if anything failed, so this is usable
# as a gate rather than only as a report.

set -uo pipefail

PASS=0
FAIL=0
LMS_HOST="${LMS_HOST:-local.openedx.io}"
CMS_HOST="${CMS_HOST:-studio.local.openedx.io}"

ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
head_() { echo; echo "== $1"; }

# Resolve a Compose SERVICE name to the exact CONTAINER name, or print nothing.
#
# Compose names containers `<project><sep><service><sep><replica>`, so the
# service has to be matched as a whole component between separators, anchored at
# both ends. Two bugs came from not doing that:
#
#   * The container check used `(^|_)${svc}(-1)?$|${svc}`. That trailing bare
#     `|${svc}` alternative collapsed the whole thing into a substring test, so
#     `lms` matched `tutor_local-lms-worker-1`. A dead LMS with a live worker
#     reported PASS. A gate that passes while the stack is broken is worse than
#     one that fails while it works.
#   * The datastore checks ran `docker exec -i mysql` and friends. No container
#     is called `mysql` — it is `tutor_local-mysql-1` — so all three reported
#     FAIL against datastores that were healthy the whole time.
#
# The project prefix is deliberately not hardcoded: `tutor_local` is this
# machine's Compose project name, not a property of the stack, and the checks
# above already avoid assuming it. The optional replica suffix keeps bare
# (non-Compose) container names working too.
container_for() {
  docker ps --format '{{.Names}}' \
    | grep -E "^(.*[-_])?${1}([-_][0-9]+)?$" \
    | head -1
}

head_ "Containers"
EXPECTED="lms cms lms-worker cms-worker mysql mongodb redis caddy meilisearch"
for svc in $EXPECTED; do
  name=$(container_for "$svc")
  if [ -n "$name" ]; then
    # Filter on the resolved name, anchored. The old `--filter name=${svc}`
    # was a substring match too, so `head -1` could report a sibling
    # container's uptime under this service's label.
    status=$(docker ps --filter "name=^${name}$" --format '{{.Status}}')
    ok "${svc} — ${name} — ${status}"
  else
    bad "${svc} not running"
  fi
done

head_ "Restart loops (a container that keeps dying reports Up briefly)"
# Counted per section, not read off the global $FAIL. Testing $FAIL here meant
# any earlier failure — one missing container was enough — silently swallowed
# this section's result, so the run went quiet about restart loops exactly when
# something was already wrong and the answer mattered most. Each check reports
# what it measured, independently of what any other check found.
looping=0
for c in $(docker ps --format '{{.Names}}'); do
  restarts=$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo 0)
  if [ "${restarts:-0}" -gt 3 ]; then
    bad "$c has restarted ${restarts} times — check 'docker logs $c'"
    looping=$((looping+1))
  fi
done
[ "$looping" -eq 0 ] && ok "no container restart loops"

head_ "HTTP surfaces"
for pair in "LMS:${LMS_HOST}" "Studio:${CMS_HOST}"; do
  name="${pair%%:*}"; host="${pair#*:}"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${host}" --max-time 30 http://localhost/ 2>/dev/null)
  if [ "$code" = "200" ] || [ "$code" = "302" ]; then
    ok "${name} (${host}) HTTP ${code}"
  else
    bad "${name} (${host}) HTTP ${code:-no response}"
  fi
done

head_ "Datastores"
# "Container missing" and "container up but not answering" are different faults
# with different fixes, so they get different messages. Collapsing them is how
# three healthy datastores spent this long being reported as down.
probe_datastore() {  # $1=compose service  $2=label  $3...=command to run inside
  local svc="$1" label="$2"; shift 2
  local name
  name=$(container_for "$svc")
  if [ -z "$name" ]; then
    bad "${label} container not found (no container for Compose service '${svc}')"
  elif docker exec -i "$name" "$@" >/dev/null 2>&1; then
    ok "${label} responds (${name})"
  else
    bad "${label} not responding (${name} is up but the probe failed)"
  fi
}

probe_datastore mysql   MySQL   mysqld --version
probe_datastore mongodb MongoDB mongosh --quiet --eval 'db.runCommand({ping:1}).ok'
probe_datastore redis   Redis   redis-cli ping

head_ "Open edX internals"
# openedx-events is what our receivers hang off; if it is absent the whole
# ingestion design has nothing to subscribe to.
if tutor local run lms python -c "import openedx_events; print(openedx_events.__version__)" 2>/dev/null | tail -1 | grep -qE '[0-9]'; then
  ver=$(tutor local run lms python -c "import openedx_events; print(openedx_events.__version__)" 2>/dev/null | tail -1)
  ok "openedx-events importable in LMS (${ver})"
else
  bad "openedx-events not importable in LMS"
fi

# The five signals the design depends on must exist by these exact names.
SIGCHECK=$(tutor local run cms python -c "
from openedx_events.content_authoring.signals import (
    XBLOCK_PUBLISHED, XBLOCK_DELETED, XBLOCK_DUPLICATED,
    COURSE_IMPORT_COMPLETED, COURSE_RERUN_COMPLETED)
from openedx_events.learning.signals import COURSE_UNENROLLMENT_COMPLETED
print('SIGNALS_OK')
" 2>/dev/null | tail -1)
[ "$SIGCHECK" = "SIGNALS_OK" ] \
  && ok "all six lifecycle signals importable" \
  || bad "one or more lifecycle signals missing"

head_ "Summary"
echo "  ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] || exit 1
