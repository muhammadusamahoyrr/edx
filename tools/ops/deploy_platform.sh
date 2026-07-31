#!/usr/bin/env bash
# Push the platform package into the running Open edX containers.
#
# Development loop only. `docker cp` into a running container does NOT survive
# `tutor local restart` or an image rebuild — the durable path is an
# `openedx-dockerfile-post-python-requirements` patch in the Tutor plugin, which
# DEPLOYMENT.md documents. Using this for a verification run is deliberate: it
# takes seconds instead of a 15-minute openedx image rebuild, and what is being
# verified is the sweep's behaviour, not the packaging.
set -eu

SRC="$HOME/cm-build/packages"
SITE=/openedx/venv/lib/python3.11/site-packages

test -d "$SRC" || { echo "FATAL: $SRC missing — run sync.sh first" >&2; exit 1; }

for c in tutor_local-cms-1 tutor_local-cms-worker-1 tutor_local-lms-1 tutor_local-lms-worker-1; do
  docker cp "$SRC/coursemate-platform/coursemate_platform" "$c:$SITE/" >/dev/null
  docker cp "$SRC/coursemate-contracts/coursemate_contracts" "$c:$SITE/" >/dev/null
  echo "$c updated"
done
