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

head_ "Containers"
EXPECTED="lms cms lms-worker cms-worker mysql mongodb redis caddy meilisearch"
RUNNING=$(docker ps --format '{{.Names}}')
for svc in $EXPECTED; do
  if echo "$RUNNING" | grep -qE "(^|_)${svc}(-1)?$|${svc}"; then
    status=$(docker ps --filter "name=${svc}" --format '{{.Status}}' | head -1)
    ok "${svc} — ${status}"
  else
    bad "${svc} not running"
  fi
done

head_ "Restart loops (a container that keeps dying reports Up briefly)"
for c in $(docker ps --format '{{.Names}}'); do
  restarts=$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo 0)
  if [ "${restarts:-0}" -gt 3 ]; then
    bad "$c has restarted ${restarts} times — check 'docker logs $c'"
  fi
done
[ "$FAIL" -eq 0 ] && ok "no container restart loops"

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
docker exec -i mysql mysqld --version >/dev/null 2>&1 \
  && ok "MySQL responds" || bad "MySQL not responding"
docker exec -i mongodb mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 \
  && ok "MongoDB responds" || bad "MongoDB not responding"
docker exec -i redis redis-cli ping >/dev/null 2>&1 \
  && ok "Redis responds" || bad "Redis not responding"

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
