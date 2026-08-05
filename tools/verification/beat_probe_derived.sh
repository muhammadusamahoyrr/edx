#!/usr/bin/env bash
# Does beat SCHEDULE and DISPATCH the sweep from an image that carries the package?
#
#   MSYS_NO_PATHCONV=1 tools/verification/beat_probe_derived.sh
#
# `beat_container_probe.sh` is the production-path probe: it starts the real
# coursemate-beat container from the openedx image. It cannot run until that
# image is rebuilt with the plugin baked in, which is measured in hours here.
#
# This gets the same evidence for the part that is actually in question, without
# the rebuild. It layers a pip install on top of the stock openedx image — no
# Python compile, no npm, no webpack, no network — and runs beat from THAT.
#
# What this proves: a container started from an image carrying the package loads
# our schedule entry, fires it, and a worker executes the dispatched task.
#
# What it does NOT prove: that `tutor images build openedx` produces the same
# image. That is a packaging question, and the Tutor plugin patch
# (openedx-dockerfile-post-python-requirements) is the standard mechanism for
# it. Run beat_container_probe.sh once the real image exists.
#
# Nothing about the running stack changes: a throwaway container on the same
# network, a settings module written and deleted here, and the deployed 03:30
# entry untouched.
set -eu

BUILD="$HOME/cm-build"
TAG=coursemate/openedx-beat-probe:test
PROBE=coursemate-beat-probe
CMS_SETTINGS="$HOME/.local/share/tutor/env/apps/openedx/settings/cms"
LMS_SETTINGS="$HOME/.local/share/tutor/env/apps/openedx/settings/lms"
CONFIG="$HOME/.local/share/tutor/env/apps/openedx/config"

cleanup() {
  docker rm -f "$PROBE" >/dev/null 2>&1 || true
  rm -f "$CMS_SETTINGS/coursemate_beat_probe.py"
}
trap cleanup EXIT

test -d "$BUILD/packages" || { echo "FATAL: $BUILD/packages missing — run sync.sh" >&2; exit 1; }

IMAGE=$(docker inspect tutor_local-cms-1 --format '{{.Config.Image}}')
NETWORK=$(docker inspect tutor_local-cms-worker-1 \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
echo "base image : $IMAGE"
echo "network    : $NETWORK"

echo
echo "=== 1. layer the package onto the stock image ==="
# --no-deps and --no-build-isolation keep this offline: every dependency the
# platform package declares is already in the edx-platform venv, and reaching
# PyPI mid-verification is how a real result turns into a network flake.
cat > "$BUILD/Dockerfile.beatprobe" <<EOF
FROM $IMAGE
USER root
COPY packages/coursemate-contracts /tmp/cmpkg/coursemate-contracts
COPY packages/coursemate-platform  /tmp/cmpkg/coursemate-platform
RUN pip install --no-deps --no-build-isolation \
      /tmp/cmpkg/coursemate-contracts /tmp/cmpkg/coursemate-platform
USER app
EOF
( cd "$BUILD" && docker build -q -f Dockerfile.beatprobe -t "$TAG" . )
rm -f "$BUILD/Dockerfile.beatprobe"

echo -n "package in the derived IMAGE: "
docker run --rm --entrypoint bash "$TAG" -c \
  'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform-*.dist-info 2>/dev/null \
   || echo "STILL NOT INSTALLED — the layer did not take"'

echo
echo "=== 2. is our schedule entry actually in the settings? ==="
# The entry comes from settings/common.py, which only runs if the app reached
# INSTALLED_APPS. Reading it back is what distinguishes "installed" from
# "installed AND discovered" — the exact gap docker cp used to leave.
docker run --rm \
  -e SERVICE_VARIANT=cms -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production \
  -v "$CMS_SETTINGS:/openedx/edx-platform/cms/envs/tutor:ro" \
  -v "$LMS_SETTINGS:/openedx/edx-platform/lms/envs/tutor:ro" \
  -v "$CONFIG:/openedx/config:ro" \
  --network "$NETWORK" --entrypoint python "$TAG" -c "
import django; django.setup()
from django.conf import settings
sched = getattr(settings, 'CELERY_BEAT_SCHEDULE', {}) or {}
print('CELERY_BEAT_SCHEDULE keys:', list(sched))
entry = sched.get('coursemate-nightly-reconcile')
print('our entry:', entry)
from django.apps import apps
# is_installed(), not a substring test on INSTALLED_APPS. The plugin registry
# inserts the AppConfig path ('coursemate_platform.apps.CourseMateConfig'), so
# \`'coursemate_platform' in settings.INSTALLED_APPS\` is False even when the app
# is loaded — a false negative that reads as a broken install.
print('app registered:', apps.is_installed('coursemate_platform'))
print('INSTALLED_APPS entry:', [a for a in settings.INSTALLED_APPS if 'coursemate' in a.lower()])
" 2>&1 | grep -vE "^\s*$|WARNING|INFO|Deferring" | tail -6

echo
echo "=== 3. run beat with the interval shortened to 30s ==="
# Inherits production and overrides ONLY the interval, so the deployed 03:30
# entry is untouched. A crontab entry hours away proves registration, not firing.
cat > "$CMS_SETTINGS/coursemate_beat_probe.py" <<'PY'
"""Throwaway: production settings with the sweep interval shortened.

Written and deleted by tools/verification/beat_probe_derived.sh so beat can be
observed firing inside a test's lifetime rather than at 03:30.
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

docker rm -f "$PROBE" >/dev/null 2>&1 || true
docker run -d --name "$PROBE" --network "$NETWORK" \
  -e SERVICE_VARIANT=cms \
  -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.coursemate_beat_probe \
  -v "$CMS_SETTINGS:/openedx/edx-platform/cms/envs/tutor:ro" \
  -v "$LMS_SETTINGS:/openedx/edx-platform/lms/envs/tutor:ro" \
  -v "$CONFIG:/openedx/config:ro" \
  --entrypoint celery "$TAG" \
  --app=cms.celery beat --loglevel=info --schedule=/tmp/probe-schedule >/dev/null

echo "beat started; watching 80s for a dispatch"
sleep 80

echo
echo "--- beat log ---"
docker logs "$PROBE" 2>&1 | grep -iE "beat: Starting|Sending due task|Error|Traceback" | head -8

echo
echo "=== 4. did a worker EXECUTE what beat dispatched? ==="
# Beat sending is half the claim. A task nobody runs is still a task nobody runs.
docker logs --since 3m tutor_local-cms-worker-1 2>&1 \
  | grep -E "reconcile_all|sweep|succeeded" | tail -6

echo
echo "=== 5. cleanup ==="
echo "(probe container and throwaway settings removed by trap)"
