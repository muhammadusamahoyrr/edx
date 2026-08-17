#!/usr/bin/env bash
# Move every Open edX container onto the freshly built image.
#
#   MSYS_NO_PATHCONV=1 tools/ops/adopt_new_image.sh
#
# **This is the operation CLAUDE.md has warned against all along, and the reason
# for that warning no longer applies.** The ban existed because the four
# containers held pip installs in their container LAYER, so recreating one
# silently reverted CourseMate to absent. The image now carries the package, so
# the install survives a recreate — it comes from the image instead.
#
# Verified before writing this: the new image contains coursemate_platform
# 0.1.0.dist-info AND the current source (multi-path transcript resolver,
# course_has_tutor, the READ-only partition helpers, staff-only filtering).
# Recreating onto an image with stale code would look like a successful upgrade
# and quietly roll the work back.
#
# Uses `tutor local start` rather than `docker compose`: tutor merges THREE
# compose files (docker-compose.yml, .prod.yml, .jobs.yml) and driving compose
# directly with one of them would recreate the containers missing the prod
# overrides.
set -eu

IMAGE=docker.io/overhangio/openedx:21.0.8-indigo

#: **Every container that runs the openedx image**, not just the four that
#: obviously do. `coursemate-beat` was missing here and is built from the same
#: image (see `deploy/tutor-plugin/coursemate.yml`), so it was never recreated
#: and never checked — it could sit on a stale image indefinitely while this
#: script reported success.
SERVICES="lms cms lms-worker cms-worker coursemate-beat"
HERE="$(cd "$(dirname "$0")" && pwd)"

#: True when every expected container is already on $NEW.
#:
#: **This used to read `tutor_local-lms-1` alone**, and that is a real defect
#: rather than a tidiness point: on 2026-08-16 an earlier command had already
#: moved `lms`, so the check saw a match, printed "nothing to do" and exited 0
#: while `lms-worker` stayed on the previous image. A partial adoption reporting
#: success is the exact failure shape this repository keeps re-finding, and the
#: verification that would have caught it (step 3) was unreachable behind this
#: early exit.
all_on_target() {
  local target="$1" c now
  for c in $SERVICES; do
    now=$(docker inspect "tutor_local-$c-1" --format '{{.Image}}' 2>/dev/null || echo "")
    [ "$now" = "$target" ] || return 1
  done
  return 0
}

echo "=== 1. preflight: is the new image actually new, and does it carry the package? ==="
NEW=$(docker images --no-trunc --format '{{.ID}}' "$IMAGE" | head -1)
echo "  latest image : ${NEW:0:19}"
for c in $SERVICES; do
  printf '  %-16s %s\n' "$c" \
    "$(docker inspect "tutor_local-$c-1" --format '{{.Image}}' 2>/dev/null | cut -c1-19)"
done
if all_on_target "$NEW"; then
  echo "  ALL expected containers are already on the latest image; nothing to do."
  exit 0
fi
docker run --rm --entrypoint bash "$IMAGE" -c \
  'ls -d /openedx/venv/lib/python3.11/site-packages/coursemate_platform-*.dist-info' \
  || { echo "FATAL: new image has no coursemate_platform. Refusing to recreate." >&2; exit 1; }

echo
echo "=== 2. recreate the four containers onto it ==="
# Compose recreates a container whose image id no longer matches, which is
# exactly the condition checked above.
tutor local start -d $SERVICES 2>&1 | tail -6

echo
echo "=== 3. did they actually move? ==="
FAILED=0
for c in $SERVICES; do
  NOW=$(docker inspect "tutor_local-$c-1" --format '{{.Image}}' 2>/dev/null || echo "")
  if [ "$NOW" = "$NEW" ]; then
    printf "  %-16s ON NEW IMAGE\n" "$c"
  elif [ -z "$NOW" ]; then
    printf "  %-16s NOT RUNNING  <-- expected container is absent\n" "$c"
    FAILED=1
  else
    printf "  %-16s STILL ON %s  <-- did not move\n" "$c" "${NOW:0:19}"
    FAILED=1
  fi
done
# Adoption is not complete until EVERY expected container matches. Reporting
# success with one behind is how a worker runs last week's code unnoticed.
[ "$FAILED" -eq 0 ] || { echo "FATAL: not all containers moved." >&2; exit 1; }

echo
echo "=== 4. wait for the CMS to answer ==="
echo -n "  "
for _ in $(seq 1 60); do
  if docker exec tutor_local-cms-1 python -c "import coursemate_platform" 2>/dev/null; then
    echo " ready"; break
  fi
  echo -n "."
  sleep 2
done

echo
echo "=== 5. the package survived the recreate (this is the whole point) ==="
bash "$HERE/check_install.sh"

echo
echo "=== 6. are the Celery tasks registered in both workers? ==="
# The failure this catches: a container that has the code on sys.path but no
# dist-info has no entry points, so the app never reaches INSTALLED_APPS and
# every enqueued task is discarded while Publish still returns 200.
bash "$HERE/check_tasks.sh"

echo
echo "=== 7. is the CURRENT code live, not just any code? ==="
docker exec tutor_local-cms-1 bash -c '
SITE=/openedx/venv/lib/python3.11/site-packages/coursemate_platform
for pat in _transcript_resolver course_has_tutor get_user_partition_groups visible_to_staff_only; do
  if grep -q "$pat" "$SITE/adapters/content_adapter.py"; then echo "  OK   $pat"; else echo "  MISS $pat"; fi
done'
