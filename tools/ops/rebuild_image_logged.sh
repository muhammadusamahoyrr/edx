#!/usr/bin/env bash
# Full openedx image rebuild, logged so a multi-hour run can be watched.
#
#   MSYS_NO_PATHCONV=1 tools/ops/rebuild_image_logged.sh
#   tail -f ~/cm-build/image-rebuild.log
#
# Wraps deploy_image.sh rather than replacing it: that script owns the staging
# rules (source into the tutor build context, plugin yml, config save) and this
# owns nothing but the log and a syncing preflight.
#
# HOURS, not minutes. The openedx image compiles Python from source and runs npm
# and webpack. CLAUDE.md lists this under "do not do without asking" for exactly
# that reason.
#
# What changes when it finishes: the image gains coursemate_platform, which is
# what the coursemate-beat container needs to start at all — a hand-install
# cannot help a container that starts later. It also means recreating the four
# openedx containers stops being destructive, because the package will come from
# the image instead of the container layer.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/cm-build/image-rebuild.log"

mkdir -p "$(dirname "$LOG")"
: > "$LOG"

{
  echo "=== started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  echo
  echo "=== preflight ==="
  echo -n "package currently in image: "
  IMAGE=$(docker inspect tutor_local-cms-1 --format '{{.Config.Image}}')
  echo -n "($IMAGE) "
  docker run --rm --entrypoint bash "$IMAGE" -c \
    'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform-*.dist-info 2>/dev/null \
     || echo NOT-INSTALLED'

  echo
  echo "=== sync working tree -> cm-build ==="
  # deploy_image.sh rsyncs FROM cm-build, so a stale cm-build would bake stale
  # code into the image and everything after would be testing the wrong thing.
  bash "$HERE/sync.sh"

  echo
  echo "=== build ==="
  bash "$HERE/deploy_image.sh"

  echo
  echo "=== finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo -n "package in the NEW image: "
  docker run --rm --entrypoint bash "$IMAGE" -c \
    'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform-*.dist-info 2>/dev/null \
     || echo STILL-NOT-INSTALLED'
} 2>&1 | tee -a "$LOG"
