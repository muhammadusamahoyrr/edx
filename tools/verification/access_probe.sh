#!/usr/bin/env bash
# Video transcripts and block-level access, against the live stack.
#
#   MSYS_NO_PATHCONV=1 tools/verification/access_probe.sh
#
# Runs probe 7 inside the CMS container, where the modulestore and the partition
# service actually exist. Writes its report to docs/ so the result is citable
# rather than scrollback.
#
# **This deploys the current working tree first.** The three things under test
# are new, so probing without deploying would measure the previous build and
# report it as today's state — which is precisely the class of mistake this
# project keeps catching.
#
# Read-only against course data: the probe creates nothing, publishes nothing and
# assigns nothing. That last one matters — see the note in the probe about
# get_user_group_id_for_partition.
set -eu

CMS=tutor_local-cms-1
COURSE="${COURSE:-course-v1:OpenedX+DemoX+DemoCourse}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$REPO/docs/Probe7_Access_And_Transcripts.md"

echo "=== 0. is the stack up? ==="
if ! docker ps --format '{{.Names}}' | grep -qx "$CMS"; then
  echo "FAIL: $CMS is not running. Start the stack before probing." >&2
  exit 1
fi
docker ps --format '  {{.Names}}\t{{.Status}}' | grep -E 'cms|lms|coursemate' || true

echo
echo "=== 1. deploy the current working tree into the CMS ==="
# Code copy only. The package already has dist-info from the real pip install, so
# entry points survive; this refreshes the .py files under it. Anything that must
# survive a container restart still needs install_workers.sh or the image.
# Both packages: `contracts` gained fields in the same change, and a platform
# that sends group_tokens to a contracts build that has never heard of them
# drops them silently rather than failing — which would read as "no block is
# restricted" and quietly pass the probe.
for PKG in coursemate_platform coursemate_contracts; do
  case "$PKG" in
    coursemate_platform) SRC="$REPO/packages/coursemate-platform/$PKG" ;;
    coursemate_contracts) SRC="$REPO/packages/coursemate-contracts/$PKG" ;;
  esac
  test -d "$SRC" || { echo "FATAL: source not found: $SRC" >&2; exit 1; }
  docker exec "$CMS" rm -rf "/tmp/cm-$PKG" >/dev/null 2>&1 || true
  docker cp "$SRC" "$CMS":"/tmp/cm-$PKG" >/dev/null
  docker exec -u root -e PKG="$PKG" "$CMS" sh -c '
    TARGET=$(python -c "import $PKG,os;print(os.path.dirname($PKG.__file__))")
    cp -r "/tmp/cm-$PKG/." "$TARGET"/
    find "$TARGET" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    echo "  refreshed: $TARGET"
  '
done

echo
echo "=== 2. stage the probe ==="
docker exec -u root "$CMS" mkdir -p /openedx/probes
docker cp "$HERE/probe_report.py" "$CMS":/openedx/probes/probe_report.py >/dev/null
docker cp "$HERE/probe_07_access_and_transcripts.py" \
          "$CMS":/openedx/probes/probe_07_access_and_transcripts.py >/dev/null

echo
echo "=== 3. run it (course: $COURSE) ==="
set +e
docker exec \
  -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production \
  -e SERVICE_VARIANT=cms \
  -e COURSEMATE_PROBE_COURSE="$COURSE" \
  "$CMS" python /openedx/probes/probe_07_access_and_transcripts.py > /tmp/probe7.md 2>/tmp/probe7.err
STATUS=$?
set -e

if [ "$STATUS" -ne 0 ]; then
  echo "FAIL: probe exited $STATUS. stderr:" >&2
  tail -40 /tmp/probe7.err >&2
  # An import error here is itself the finding — most likely _TRANSCRIPT_MODULES
  # or the partitions import not matching this release.
  exit "$STATUS"
fi

cp /tmp/probe7.md "$OUT"
echo "  report written: docs/$(basename "$OUT")"

echo
echo "=== 4. the four lines that decide what to do next ==="
grep -E "transcript resolver found|resolver module|leaves by type|group-restricted leaf blocks|user_group_tokens|active partitions on course|course_has_tutor" \
  "$OUT" || echo "  (evidence table not matched — read the report)"

echo
echo "=== 5. conclusions ==="
sed -n '/### Conclusion/,/^###/p' "$OUT" | grep -E '^\- \*\*' || true

echo
echo "Full report: $OUT"
