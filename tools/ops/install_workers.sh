#!/usr/bin/env bash
# Install the platform package properly in the worker containers.
#
# `docker cp` puts the code on the path but installs no dist-info, so pip's
# entry points are absent — and `lms.djangoapp` / `cms.djangoapp` entry points
# are exactly how Open edX discovers a plugin app. Without them the app never
# reaches INSTALLED_APPS, so Django never loads it, Celery never autodiscovers
# `coursemate_platform.tasks`, and every enqueued task is rejected with
# "Received unregistered task". A real install is required, not a file copy.
set -eu

SRC="$HOME/cm-build/packages"
test -d "$SRC" || { echo "FATAL: $SRC missing — run sync.sh first" >&2; exit 1; }

for c in tutor_local-cms-worker-1 tutor_local-lms-worker-1; do
  docker exec "$c" rm -rf /tmp/cmpkg
  docker exec "$c" mkdir -p /tmp/cmpkg
  docker cp "$SRC/coursemate-contracts" "$c:/tmp/cmpkg/" >/dev/null
  docker cp "$SRC/coursemate-platform" "$c:/tmp/cmpkg/" >/dev/null
  # --no-build-isolation keeps this offline: the build backend is already in the
  # venv, and reaching out to PyPI mid-verification is how a "works locally"
  # result turns into a network flake.
  docker exec "$c" pip install --no-deps --no-build-isolation \
    /tmp/cmpkg/coursemate-contracts /tmp/cmpkg/coursemate-platform 2>&1 | tail -2
  echo "$c installed"
done
