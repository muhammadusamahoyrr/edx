#!/usr/bin/env bash
set -eu
for c in tutor_local-cms-1 tutor_local-cms-worker-1 tutor_local-lms-1 tutor_local-lms-worker-1; do
  echo -n "$c: "
  docker exec "$c" bash -c 'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform*.dist-info 2>/dev/null | head -1 || echo "NO dist-info"'
done
