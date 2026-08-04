#!/usr/bin/env bash
# Attach a transcript to one DemoX video, then prove it reaches the index.
#
#   MSYS_NO_PATHCONV=1 tools/verification/add_test_transcript.sh
#
# WRITES COURSE DATA — one video, one language. See the .py for how to undo it.
set -eu

CMS=tutor_local-cms-1
CM=tutor_local-coursemate-1
COURSE="${COURSE:-course-v1:OpenedX+DemoX+DemoCourse}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. attach the transcript and check the extractor ==="
docker exec "$CMS" mkdir -p /openedx/probes
docker cp "$HERE/add_test_transcript.py" "$CMS":/openedx/probes/add_test_transcript.py >/dev/null
docker exec \
  -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production \
  -e SERVICE_VARIANT=cms \
  -e COURSEMATE_PROBE_COURSE="$COURSE" \
  "$CMS" python /openedx/probes/add_test_transcript.py 2>&1 | grep -vE "^\s*$" | tail -25

echo
echo "=== 2. reindex so the video chunk actually reaches the served index ==="
# A transcript the extractor can read but that never gets indexed is still
# invisible to a student, so the claim is only settled after this.
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms coursemate_reindex \
  --course "$COURSE" --inline 2>/dev/null | tail -3

echo
echo "=== 3. is the video chunk retrievable? ==="
cat > /tmp/cm_video_check.py <<'PY'
from coursemate_service.config import settings
from coursemate_service.knowledge import get_store
import os

offering = os.environ["OFFERING"]
s = get_store()

rows = list(s._conn.execute(
    "SELECT usage_key, display_name, LENGTH(text) n FROM chunks "
    "WHERE offering_id=? AND active=1 AND block_type='video'", (offering,)))
print("video chunks in the served index:", len(rows))
for r in rows:
    print(f"   {r['display_name']!r}  {r['n']} chars  {r['usage_key']}")

# Retrieval end to end, on a phrase that appears ONLY in the transcript.
hits = s.search("campus-wide deployments", tenant=settings.tenant,
                offering_id=offering, limit=5)
print("\nquery 'campus-wide deployments' ->", len(hits), "hit(s)")
for h in hits:
    print(f"   {h.display_name!r} score={h.score:.3f}")
print("\nRESULT:", "PASS" if rows and hits else "FAIL")
PY
docker cp /tmp/cm_video_check.py "$CM":/tmp/cm_video_check.py >/dev/null
docker exec -e OFFERING="$COURSE" "$CM" python /tmp/cm_video_check.py
