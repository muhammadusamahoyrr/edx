#!/usr/bin/env bash
# Push the XBlock's CSS / HTML / JS straight into the running containers.
#
# Why this exists: the fragment inlines all three at render time
# (`add_css` / `add_javascript` / `render_django_template` all go through
# `loader.load_unicode`), so a file copy plus a page reload IS the deployment.
# No bundler, no `collectstatic`, no container restart.
#
# The alternative is a 29-minute openedx image rebuild for a CSS change, which
# is how you stop iterating on the UI altogether.
#
# NOT permanent. The files live in the container's site-packages, not the
# image, so `tutor local restart` or a machine reboot reverts the running stack
# to whatever the image holds. Your working tree is untouched either way. To
# make it permanent, rebuild and adopt the openedx image.
#
# Run from WSL:
#   bash tools/ops/push_ui_assets.sh
#
# A script rather than an inline `wsl -- bash -c '…'`: the repo path contains a
# space ("The Laptop Hut"), and inline WSL invocations lose the quoting — the
# copy then fails with "docker cp requires 2 arguments". See CLAUDE.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$HERE/packages/coursemate-platform/coursemate_platform/xblock/static"
DST="/openedx/venv/lib/python3.11/site-packages/coursemate_platform/xblock/static"

if [ ! -f "$SRC/css/tutor.css" ]; then
  echo "FAIL: no assets at $SRC" >&2
  exit 1
fi

for c in lms cms; do
  name="tutor_local-${c}-1"
  if ! docker inspect "$name" >/dev/null 2>&1; then
    echo "  skip $c (container not running)"
    continue
  fi
  docker cp "$SRC/css/tutor.css"          "$name:$DST/css/tutor.css"
  docker cp "$SRC/html/student_view.html" "$name:$DST/html/student_view.html"
  docker cp "$SRC/html/studio_view.html"  "$name:$DST/html/studio_view.html"
  docker cp "$SRC/js/src/tutor.js"        "$name:$DST/js/src/tutor.js"
  docker cp "$SRC/js/src/studio.js"       "$name:$DST/js/src/studio.js"
  echo "  pushed to $c"
done

# Read the files back out of the container rather than trusting the copy. A
# silent no-op here looks exactly like a browser cache problem, and you can
# lose an afternoon to that.
echo "--- verified inside the LMS container ---"
printf "  app bar in template   : %s\n" \
  "$(docker exec tutor_local-lms-1 grep -c 'cm-appbar'   "$DST/html/student_view.html")"
printf "  plan bar in js        : %s\n" \
  "$(docker exec tutor_local-lms-1 grep -c 'cm-plan-bar' "$DST/js/src/tutor.js")"
printf "  design tokens in css  : %s\n" \
  "$(docker exec tutor_local-lms-1 grep -c 'cm-teal'     "$DST/css/tutor.css")"

echo
echo "Now hard-refresh the page (Ctrl+F5) — the old CSS is inlined in the"
echo "cached HTML, so an ordinary reload can still show the previous design."
