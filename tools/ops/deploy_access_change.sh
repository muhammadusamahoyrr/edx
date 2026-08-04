#!/usr/bin/env bash
# Take the transcript + block-access + opt-in change from "passes tests" to
# "running on this stack".
#
#   MSYS_NO_PATHCONV=1 tools/ops/deploy_access_change.sh
#   MSYS_NO_PATHCONV=1 tools/ops/deploy_access_change.sh --skip-reindex
#
# Three halves have to move together, and each is useless alone:
#
#   SERVICE   store.py grew a `chunk_groups` table and the query that reads it.
#             The running container's SQLite has no such table, so the access
#             filter matches nothing and every chunk reads as unrestricted.
#   PLATFORM  content_adapter now emits group tokens and the XBlock mints them.
#             Only the CMS was refreshed during probing, so the LMS still mints
#             tokens with no groups -- and a caller with no groups sees only
#             unrestricted content. Half-deployed, the filter DENIES correctly
#             for the wrong reason, which looks identical to working.
#   INDEX     the 221 live chunks were written before group_tokens existed. Until
#             a reindex, the two restricted DemoX blocks are served to everyone.
#
# Order matters: schema before data, code before the run that uses it.
#
# What this does NOT do, deliberately: no `tutor local restart`, no
# `--force-recreate` on any openedx container, no openedx image rebuild. The four
# openedx containers hold pip installs in their container LAYER; recreating one
# silently reverts CourseMate to absent. Only `docker restart` is used on them,
# which preserves the layer.
set -eu

SKIP_REINDEX=0
[ "${1:-}" = "--skip-reindex" ] && SKIP_REINDEX=1

BUILD="$HOME/cm-build"
COURSE="${COURSE:-course-v1:OpenedX+DemoX+DemoCourse}"
SERVICE_IMAGE="coursemate/service:0.1.0"
CM=tutor_local-coursemate-1
OPENEDX="tutor_local-cms-1 tutor_local-cms-worker-1 tutor_local-lms-1 tutor_local-lms-worker-1"

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== 0. preflight ==="
test -d "$BUILD/packages" || { echo "FATAL: $BUILD/packages missing — run sync.sh first" >&2; exit 1; }
for c in $CM $OPENEDX; do
  docker ps --format '{{.Names}}' | grep -qx "$c" \
    || { echo "FATAL: $c is not running" >&2; exit 1; }
done
echo "  all 5 containers up"

echo
echo "=== 1. sync working tree into WSL ==="
bash "$HERE/sync.sh"

echo
echo "=== 2. rebuild the service image (offline, ~20s) ==="
# Build context is the repo root because Dockerfile.service COPYs packages/.
# Dependencies live in coursemate/deps:1 and are untouched, so this is a source
# layer only -- seconds, and no network.
( cd "$BUILD" && docker build -q -f deploy/Dockerfile.service -t "$SERVICE_IMAGE" . )
echo "  built $SERVICE_IMAGE"

echo
echo "=== 3. recreate the coursemate container onto the new image ==="
# `docker restart` would keep the OLD image, so this one genuinely has to be a
# recreate. Safe here and nowhere else: this container holds no manual installs
# (everything is in the image) and the index lives on a volume, so nothing is
# lost. `--no-deps` keeps compose from touching mysql, mongo or redis.
ENVROOT="$(tutor config printroot)/env/local"
if docker compose --project-directory "$ENVROOT" -f "$ENVROOT/docker-compose.yml" \
     up -d --no-deps --force-recreate coursemate 2>/dev/null; then
  echo "  recreated via compose"
else
  # Older tutor layouts name the project differently; fall back to tutor itself,
  # which knows its own compose files.
  echo "  compose path failed, falling back to: tutor local start -d coursemate"
  tutor local start -d coursemate
fi

echo -n "  waiting for health: "
for _ in $(seq 1 30); do
  if docker exec "$CM" python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/coursemate/health'); sys.exit(0)" 2>/dev/null; then
    echo "ok"; break
  fi
  echo -n "."
  sleep 2
done

echo
echo "=== 4. confirm the new schema actually exists ==="
# The table is created by CREATE TABLE IF NOT EXISTS at ChunkStore init, so its
# ABSENCE here means the container is still on the old image -- the one failure
# this whole script exists to prevent, and it is invisible from the outside.
cat > /tmp/cm_schema.py <<'PY'
from coursemate_service.knowledge import get_store
s = get_store()
tables = [r[0] for r in s._conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", tables)
assert "chunk_groups" in tables, "chunk_groups MISSING — container is on the old image"
print("chunk_groups rows:", s._conn.execute("SELECT COUNT(*) FROM chunk_groups").fetchone()[0])
PY
docker cp /tmp/cm_schema.py "$CM":/tmp/cm_schema.py >/dev/null
docker exec "$CM" python /tmp/cm_schema.py

echo
echo "=== 5. platform code into all four openedx containers ==="
bash "$HERE/deploy_platform.sh"

echo
echo "=== 6. restart them so the new code is imported ==="
# Python caches modules at process start, so a file copy alone changes nothing
# in a running worker. `docker restart` preserves the container layer -- unlike
# `docker compose up --force-recreate`, which would discard the pip installs.
for c in $OPENEDX; do
  docker restart "$c" >/dev/null
  echo "  restarted $c"
done

echo -n "  waiting for CMS to answer: "
for _ in $(seq 1 45); do
  if docker exec tutor_local-cms-1 python -c "import coursemate_platform" 2>/dev/null; then
    echo "ok"; break
  fi
  echo -n "."
  sleep 2
done

if [ "$SKIP_REINDEX" -eq 1 ]; then
  echo
  echo "=== 7. SKIPPED (--skip-reindex) ==="
  echo "  NOTE: until a reindex runs, live chunks carry no group tokens and the"
  echo "        two restricted DemoX blocks are still served to every caller."
  exit 0
fi

echo
echo "=== 7. reindex $COURSE ==="
# Safe by construction (§5.3): chunks are written INACTIVE under a new version,
# verified, and only then does the active pointer flip. A failure here leaves the
# current index serving exactly as it is now.
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms coursemate_reindex \
  --course "$COURSE" --inline

echo
echo "=== 8. what landed ==="
cat > /tmp/cm_after.py <<'PY'
from coursemate_service.knowledge import get_store
s = get_store()
rows = list(s._conn.execute(
    "SELECT offering_id, COUNT(*) c, SUM(active) a FROM chunks GROUP BY offering_id"))
print("chunks by offering:", [dict(r) for r in rows] or "EMPTY")
print("restricted chunks :", s._conn.execute(
    "SELECT COUNT(DISTINCT chunk_id) FROM chunk_groups").fetchone()[0])
for r in s._conn.execute(
        "SELECT g.group_token, COUNT(*) n FROM chunk_groups g GROUP BY g.group_token"):
    print(f"   {r[0]}: {r[1]} chunk(s)")
PY
docker cp /tmp/cm_after.py "$CM":/tmp/cm_after.py >/dev/null
docker exec "$CM" python /tmp/cm_after.py

echo
echo "Done. Expect a non-zero 'restricted chunks' count — probe 7 found 2"
echo "group-restricted blocks in DemoX. Zero means group tokens are not"
echo "reaching the index, and the filter is protecting nothing."
