#!/usr/bin/env bash
# Does anything actually schedule the sweep?
#
# A CELERY_BEAT_SCHEDULE entry that no scheduler reads is a comment, not a
# nightly job — and the sweep is the only mitigation for unpublished content, so
# "scheduled" has to be demonstrated rather than declared.
set -eu

echo "=== 1. is our entry in the CMS beat schedule? ==="
cat > /tmp/sched.py <<'PY'
from django.conf import settings
sched = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
entry = sched.get("coursemate-nightly-reconcile")
print("entry:", entry or "ABSENT")
PY
docker cp /tmp/sched.py tutor_local-cms-worker-1:/tmp/sched.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-worker-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/sched.py').read())" 2>&1 | grep "^entry:"

echo
echo "=== 2. does beat load it? (60s run, then stopped) ==="
docker exec tutor_local-cms-worker-1 rm -f /openedx/data/beat-probe-schedule
timeout 60 docker exec tutor_local-cms-worker-1 \
  celery --app=cms.celery beat --loglevel=debug \
  --schedule=/openedx/data/beat-probe-schedule 2>&1 \
  | grep -iE "coursemate|Scheduler: Sending|beat: Starting" | head -8 || true

echo
echo "=== 3. run the nightly task itself ==="
cat > /tmp/ra.py <<'PY'
from coursemate_platform.tasks.reconcile import reconcile_all
print("result:", reconcile_all.delay().get(timeout=90))
PY
docker cp /tmp/ra.py tutor_local-cms-worker-1:/tmp/ra.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-worker-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/ra.py').read())" 2>&1 | grep "^result:"

echo
echo "=== 4. worker log ==="
docker logs --since 2m tutor_local-cms-worker-1 2>&1 | grep -iE "sweep|reconcile_all" | tail -6
