#!/usr/bin/env bash
# Deploy the Redis-backed shared state and the grounding default.
#
#   MSYS_NO_PATHCONV=1 tools/ops/deploy_shared_state.sh
#
# Service-only: nothing here touches the openedx containers, so it is a ~20s
# offline image rebuild and one container recreate.
#
# Redis DB **1**, not 0. Celery's broker is on 0 for the whole Open edX
# deployment; sharing a keyspace with it would put our rate-limit keys next to
# the platform's queues, where a careless FLUSHDB during debugging takes out
# both. Separate logical DB, same server, no new infrastructure.
set -eu

BUILD="$HOME/cm-build"
CM=tutor_local-coursemate-1
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. sync + rebuild the service image ==="
bash "$HERE/sync.sh"
( cd "$BUILD" && docker build -q -f deploy/Dockerfile.service -t coursemate/service:0.1.0 . >/dev/null )
echo "  rebuilt"

echo
echo "=== 2. install the updated plugin, THEN point the service at redis ==="
# The plugin file has to be copied first. `--set` on a key the installed plugin
# does not declare is silently dropped: config.yml gets the value, the compose
# template never references it, and the container starts with an empty setting —
# which then degrades to per-process state without complaining, because that is
# exactly what it is designed to do when redis_url is blank.
cp "$BUILD/deploy/tutor-plugin/coursemate.yml" "$(tutor plugins printroot)/"
echo "  plugin copied"
tutor config save --set COURSEMATE_REDIS_URL="redis://redis:6379/1" 2>&1 | tail -2

echo
echo "=== 3. recreate the service container ==="
# `tutor local start`, NOT raw docker compose. Tutor merges three compose files
# and driving one of them alone fails with "coursemate-beat depends on undefined
# service cms-worker" — cms-worker lives in docker-compose.prod.yml. Worse, that
# failure is non-fatal in a pipeline: the old container keeps running and every
# check afterwards reports on code that was never deployed.
docker rm -f "$CM" >/dev/null 2>&1 || true
tutor local start -d coursemate 2>&1 | tail -3

echo -n "  waiting for health: "
for _ in $(seq 1 30); do
  if docker exec "$CM" python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/coursemate/health'); sys.exit(0)" 2>/dev/null; then
    echo "ok"; break
  fi
  echo -n "."; sleep 2
done

echo
echo "=== 4. did the settings actually land? ==="
docker exec "$CM" python -c "
from coursemate_service.config import settings
print('  require_grounding :', settings.require_grounding)
print('  redis_url         :', settings.redis_url or '<empty>')
"

echo
echo "=== 5. is redis REACHED, or did it quietly fall back to per-process? ==="
# The distinction that matters: shared_state degrades silently by design, so
# "no error" is not evidence it is working. Ask it directly.
docker exec "$CM" python -c "
from coursemate_service import shared_state
c = shared_state.get_redis()
if c is None:
    print('  NOT CONNECTED -- running per-process (check the container log)')
    raise SystemExit(1)
print('  connected:', c.ping())
c.setex('cm:selftest', 10, 'ok')
print('  round trip:', c.get('cm:selftest'))
print('  our keyspace is db1, celery is db0 -- keys here:', len(list(c.scan_iter(match='cm:*'))))
"

echo
echo "=== 6. rate limit really shared? two limiters, one budget ==="
docker exec "$CM" python -c "
from coursemate_service.api.deps import _RateLimiter
from coursemate_service.config import settings
from fastapi import HTTPException
import uuid
sid = 'selftest-' + uuid.uuid4().hex[:8]
a, b = _RateLimiter(), _RateLimiter()          # two 'replicas'
n = settings.student_requests_per_minute
for i in range(n):
    (a if i % 2 == 0 else b).check(sid)        # alternate between them
try:
    b.check(sid)
    print(f'  FAIL: {n+1} requests allowed across two limiters')
except HTTPException as e:
    print(f'  OK: blocked at {n+1} across TWO limiter instances ({e.status_code})')
"
