#!/usr/bin/env bash
# Does the coursemate-beat CONTAINER actually schedule and dispatch the sweep?
#
# The schedule was already proven under a beat process inside a container that
# had the package hand-installed. This checks the thing that runs in production:
# a container started from the openedx image, which is only useful if the image
# itself carries the package. That distinction is the whole point — a
# hand-install cannot help any container that starts later.
set -eu

BEAT=tutor_local-coursemate-beat-1
IMAGE=$(docker inspect tutor_local-cms-1 --format '{{.Config.Image}}')
CMS_SETTINGS="$HOME/.local/share/tutor/env/apps/openedx/settings/cms"

echo "=== 1. is the package IN the image, not copied into a container? ==="
docker run --rm --entrypoint bash "$IMAGE" -c \
  'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform-*.dist-info 2>/dev/null \
   || echo "NOT INSTALLED IN IMAGE"'

echo
echo "=== 2. start the beat container ==="
tutor local start -d coursemate-beat 2>&1 | tail -3
sleep 20
docker ps --filter "name=coursemate-beat" --format "{{.Names}}  {{.Status}}"

echo
echo "=== 3. did it load our entry, and is the app installed in it? ==="
docker logs "$BEAT" 2>&1 | grep -iE "beat: Starting|Configuration|Error|Traceback" | head -6
docker exec "$BEAT" python -c \
  "import importlib.metadata as md; print('coursemate-platform', md.version('coursemate-platform'))" 2>&1 | tail -1

echo
echo "=== 4. prove it DISPATCHES ==="
# A crontab entry hours away proves registration, not firing. This probe settings
# module inherits production and overrides only the interval, so the deployed
# 03:30 entry is untouched and nothing about the running stack changes.
cat > "$CMS_SETTINGS/coursemate_beat_probe.py" <<'PY'
"""Throwaway: production settings with the sweep interval shortened.

Exists so beat can be observed firing within a test's lifetime instead of at
03:30. Written by tools/verification/beat_container_probe.sh and deleted by it.
"""
from datetime import timedelta

from .production import *  # noqa: F401,F403

CELERY_BEAT_SCHEDULE = {
    "coursemate-nightly-reconcile": {
        "task": "coursemate_platform.tasks.reconcile.reconcile_all",
        "schedule": timedelta(seconds=30),
    }
}
CELERYBEAT_SCHEDULE = CELERY_BEAT_SCHEDULE
PY

docker rm -f coursemate-beat-probe >/dev/null 2>&1 || true
docker run --rm -d --name coursemate-beat-probe \
  --network "$(docker inspect "$BEAT" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')" \
  -e SERVICE_VARIANT=cms \
  -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.coursemate_beat_probe \
  -v "$CMS_SETTINGS:/openedx/edx-platform/cms/envs/tutor:ro" \
  -v "$HOME/.local/share/tutor/env/apps/openedx/settings/lms:/openedx/edx-platform/lms/envs/tutor:ro" \
  -v "$HOME/.local/share/tutor/env/apps/openedx/config:/openedx/config:ro" \
  "$IMAGE" \
  celery --app=cms.celery beat --loglevel=info --schedule=/tmp/probe-schedule >/dev/null

echo "probe beat running; watching 75s for a dispatch"
sleep 75
docker logs coursemate-beat-probe 2>&1 | grep -iE "Sending due task|Error|Traceback" | head -5
docker rm -f coursemate-beat-probe >/dev/null 2>&1 || true
rm -f "$CMS_SETTINGS/coursemate_beat_probe.py"

echo
echo "=== 5. did the worker execute what beat dispatched? ==="
docker logs --since 2m tutor_local-cms-worker-1 2>&1 \
  | grep -E "reconcile_all|sweep course" | tail -4
