#!/usr/bin/env bash
# Phase 1 finalisation: admin user, demo course, and the sign-off checklist.
#
#   wsl -d Ubuntu-24.04 -- bash -s < tools/verification/phase1_finalize.sh
#
# Run only after LMS and Studio return 200/302 through Caddy. Everything here
# assumes a serving platform; running it earlier produces misleading failures.

set -uo pipefail

LMS_HOST="local.openedx.io"
CMS_HOST="studio.local.openedx.io"
PORT="${CADDY_PORT:-8080}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASS="${ADMIN_PASS:-adminpass123}"

PASS=0; FAIL=0
ok()  { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=============================================================="
echo " PHASE 1 FINALISATION"
echo "=============================================================="

echo
echo "-- creating admin user --"
# Idempotent: re-running must not fail the script.
tutor local do createuser --staff --superuser \
  "${ADMIN_USER}" "${ADMIN_EMAIL}" -p "${ADMIN_PASS}" 2>&1 | tail -3 \
  || echo "  (user likely exists already — continuing)"

echo
echo "-- importing DemoX --"
tutor local do importdemocourse 2>&1 | tail -5

echo
echo "=============================================================="
echo " SIGN-OFF CHECKLIST"
echo "=============================================================="

echo
echo "1. Containers"
EXPECTED="lms cms lms-worker cms-worker mysql mongodb redis caddy meilisearch"
RUNNING=$(docker ps --format '{{.Names}}')
for svc in $EXPECTED; do
  if echo "$RUNNING" | grep -q -- "-${svc}-1"; then
    restarts=$(docker inspect "$(echo "$RUNNING" | grep -m1 -- "-${svc}-1")" --format '{{.RestartCount}}')
    if [ "${restarts:-0}" -le 2 ]; then
      ok "${svc} running (restarts=${restarts})"
    else
      bad "${svc} restarting repeatedly (restarts=${restarts})"
    fi
  else
    bad "${svc} not running"
  fi
done

echo
echo "2. HTTP surfaces (through Caddy)"
for pair in "LMS:${LMS_HOST}" "Studio:${CMS_HOST}"; do
  name="${pair%%:*}"; host="${pair#*:}"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${host}" --max-time 30 "http://127.0.0.1:${PORT}/")
  case "$code" in
    200|302) ok "${name} HTTP ${code}" ;;
    *)       bad "${name} HTTP ${code}" ;;
  esac
done

echo
echo "3. Datastores"
docker exec -i "$(docker ps -qf name=mysql)" mysqladmin ping >/dev/null 2>&1 \
  && ok "MySQL responds" || bad "MySQL not responding"
docker exec -i "$(docker ps -qf name=mongodb)" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 \
  && ok "MongoDB responds" || bad "MongoDB not responding"
docker exec -i "$(docker ps -qf name=redis)" redis-cli ping >/dev/null 2>&1 \
  && ok "Redis responds" || bad "Redis not responding"

echo
echo "4. Admin user exists"
tutor local run lms python -c "
import django; django.setup()
from django.contrib.auth import get_user_model
u = get_user_model().objects.filter(username='${ADMIN_USER}').first()
print('FOUND' if u and u.is_superuser else 'MISSING')
" 2>/dev/null | grep -q FOUND && ok "superuser '${ADMIN_USER}' exists" || bad "superuser missing"

echo
echo "5. DemoX present"
tutor local run cms python -c "
import django; django.setup()
from opaque_keys.edx.keys import CourseKey
from xmodule.modulestore.django import modulestore
c = modulestore().get_course(CourseKey.from_string('course-v1:OpenedX+DemoX+DemoCourse'))
print('FOUND' if c else 'MISSING')
" 2>/dev/null | grep -q FOUND && ok "DemoX imported" || bad "DemoX missing"

echo
echo "6. Lifecycle signals importable (the ingestion design depends on all six)"
tutor local run cms python -c "
import django; django.setup()
from openedx_events.content_authoring.signals import (
    XBLOCK_PUBLISHED, XBLOCK_DELETED, XBLOCK_DUPLICATED,
    COURSE_IMPORT_COMPLETED, COURSE_RERUN_COMPLETED)
from openedx_events.learning.signals import COURSE_UNENROLLMENT_COMPLETED
print('SIGNALS_OK')
" 2>/dev/null | grep -q SIGNALS_OK && ok "all six signals importable" || bad "signals missing"

echo
echo "=============================================================="
echo "  ${PASS} passed, ${FAIL} failed"
echo "  LMS:    http://${LMS_HOST}:${PORT}"
echo "  Studio: http://${CMS_HOST}:${PORT}"
echo "=============================================================="
[ "$FAIL" -eq 0 ] || exit 1
