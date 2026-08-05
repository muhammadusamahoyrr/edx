#!/usr/bin/env bash
# Import a second course, so course isolation is OBSERVED rather than asserted.
#
#   MSYS_NO_PATHCONV=1 tools/verification/import_second_course.sh
#
# Every isolation check so far has proved that a query scoped to DemoX returns
# DemoX content, in an index where DemoX is the ONLY content. The filter has
# never had anything to exclude. That is a weaker claim than it reads as.
#
# Course: "Intro to Open edX" from openedx/training-courses, CC BY-SA 4.0, and
# CI-tested by upstream to import into a freshly provisioned Tutor instance.
#
# **Writes course data**: creates course-v1:OpenedX+OEX101+2023 in Studio. To
# undo, delete it from Studio and re-run coursemate_reindex.
#
# Deliberately NOT given a tutor block. That buys a second proof for free: the
# course is indexed explicitly with --course, and `--all` must then SKIP it,
# because opt-in is the presence of the block.
set -eu

CMS=tutor_local-cms-1
CM=tutor_local-coursemate-1
COURSE2="course-v1:OpenedX+OEX101+2023"
DEMOX="course-v1:OpenedX+DemoX+DemoCourse"
URL="https://raw.githubusercontent.com/openedx/training-courses/main/dist/Intro%20To%20Open%20edX%20Course.tar.gz"

echo "=== 1. fetch the archive ==="
if [ ! -f /tmp/intro.tar.gz ]; then
  curl -sL -o /tmp/intro.tar.gz "$URL"
fi
echo "  $(stat -c%s /tmp/intro.tar.gz) bytes"
tar tzf /tmp/intro.tar.gz >/dev/null || { echo "FATAL: not a valid archive" >&2; exit 1; }

echo
echo "=== 2. stage and extract inside the CMS ==="
docker exec -u root "$CMS" rm -rf /tmp/import
docker exec -u root "$CMS" mkdir -p /tmp/import
docker cp /tmp/intro.tar.gz "$CMS":/tmp/import/intro.tar.gz >/dev/null
# The archive's top-level directory is ./course, so extraction gives
# /tmp/import/course — which is the course_dir the import command wants.
docker exec -u root "$CMS" tar xzf /tmp/import/intro.tar.gz -C /tmp/import
docker exec -u root "$CMS" chown -R app:app /tmp/import
docker exec "$CMS" ls /tmp/import | sed 's/^/  /'

echo
echo "=== 3. import (headless — no Studio UI) ==="
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms import /tmp/import course 2>&1 | tail -8

echo
echo "=== 4. courses on the instance now ==="
cat > /tmp/lc.py <<'PY'
from coursemate_platform.adapters import content_adapter as ca
for k in ca.list_course_keys():
    print(f"   {k}   tutor_block={ca.course_has_tutor(k)}")
PY
docker cp /tmp/lc.py "$CMS":/tmp/lc.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/lc.py').read())" 2>&1 \
  | grep "course-v1"

echo
echo "=== 5. index the second course EXPLICITLY (--course bypasses opt-in) ==="
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms coursemate_reindex \
  --course "$COURSE2" --inline 2>/dev/null | tail -3

echo
echo "=== 6. does --all still SKIP it? (opt-in, with something to skip) ==="
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms coursemate_reindex --all 2>/dev/null \
  | grep -E "opted in|Indexing" | head -4

echo
echo "=== 7. what the index holds now ==="
cat > /tmp/d2.py <<'PY'
from coursemate_service.knowledge import get_store
s = get_store()
for r in s._conn.execute(
        "SELECT offering_id, COUNT(*) c, SUM(active) a FROM chunks GROUP BY offering_id"):
    print(f"   {r['offering_id']}   chunks={r['c']} active={r['a']}")
PY
docker cp /tmp/d2.py "$CM":/tmp/d2.py >/dev/null
docker exec "$CM" python /tmp/d2.py

echo
echo "=== 8. THE TEST: can a query in one course reach the other? ==="
cat > /tmp/iso.py <<'PY'
import os
from coursemate_service.config import settings
from coursemate_service.knowledge import get_store

A = os.environ["COURSE_A"]; B = os.environ["COURSE_B"]
s = get_store()

def titles(offering, q, limit=8):
    return [(c.display_name or "?") for c in
            s.search(q, tenant=settings.tenant, offering_id=offering, limit=limit)]

# Terms drawn from the OTHER course, so a leak would be visible rather than
# theoretical. A generic query would return nothing from either and "pass".
probes = [
    ("periodic table elements chemistry", A),   # DemoX has an interactive periodic table
    ("contribute open source community",  B),   # the training course is about contributing
]
fails = 0
for q, home in probes:
    other = B if home == A else A
    in_home  = titles(home, q)
    in_other = titles(other, q)
    print(f"\n  query: {q!r}")
    print(f"    in its own course   ({home.split('+')[1]}): {len(in_home)} hit(s) {in_home[:3]}")
    print(f"    in the OTHER course ({other.split('+')[1]}): {len(in_other)} hit(s) {in_other[:3]}")

# The real assertion: every row returned for a course must BELONG to it.
for offering in (A, B):
    rows = s._conn.execute(
        "SELECT DISTINCT c.offering_id FROM chunks_fts "
        "JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ? AND c.offering_id = ? AND c.active = 1",
        ("open OR course OR content OR learner", offering)).fetchall()
    got = [r[0] for r in rows]
    ok = got in ([offering], [])
    print(f"\n  offerings actually returned for {offering.split('+')[1]}: {got or 'none'}  "
          f"{'OK' if ok else 'LEAK'}")
    if not ok:
        fails += 1

print("\n  RESULT:", "PASS — no cross-course leakage" if not fails else f"FAIL ({fails})")
raise SystemExit(1 if fails else 0)
PY
docker cp /tmp/iso.py "$CM":/tmp/iso.py >/dev/null
docker exec -e COURSE_A="$DEMOX" -e COURSE_B="$COURSE2" "$CM" python /tmp/iso.py
