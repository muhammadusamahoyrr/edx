#!/usr/bin/env bash
# Does the ENQUEUED bootstrap path actually activate what it writes?
#
#   MSYS_NO_PATHCONV=1 tools/verification/bootstrap_swap_probe.sh
#
# It did not. `bootstrap_course` called send_leaves without run_id or is_final,
# so the service never verified and never swapped: every chunk landed INACTIVE,
# the course kept serving the previous version, and each run leaked a whole copy
# of the course into the index. The task returned indexed==total throughout,
# because that counts what the service ACCEPTED, not what became active.
#
# The only reason it surfaced is that importing a second course made the totals
# stop matching: 454 chunks across 2 versions with 227 active.
#
# This probe drives the real Celery task -- not the --inline path, which always
# set both flags and is why every earlier reindex looked fine.
set -eu

CMS=tutor_local-cms-1
CM=tutor_local-coursemate-1
COURSE="${COURSE:-course-v1:OpenedX+DemoX+DemoCourse}"
HERE="$(cd "$(dirname "$0")/../ops" && pwd)"

echo "=== 1. deploy the fix + migrate ==="
bash "$HERE/sync.sh" | tail -1
bash "$HERE/deploy_platform.sh" | tail -2
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$CMS" \
  python /openedx/edx-platform/manage.py cms migrate coursemate_platform 2>&1 | tail -3
for c in tutor_local-cms-1 tutor_local-cms-worker-1; do docker restart "$c" >/dev/null; done
echo -n "  waiting for the worker: "
for _ in $(seq 1 45); do
  docker exec tutor_local-cms-worker-1 python -c "import coursemate_platform" 2>/dev/null && { echo ok; break; }
  echo -n "."; sleep 2
done
sleep 20   # let celery finish registering before we enqueue

echo
echo "=== 2. state BEFORE ==="
cat > /tmp/before.py <<'PY'
from coursemate_service.knowledge import get_store
s = get_store()
for r in s._conn.execute(
        "SELECT offering_id, COUNT(*) c, SUM(active) a, COUNT(DISTINCT version) v "
        "FROM chunks GROUP BY offering_id"):
    print(f"   {r['offering_id']}  chunks={r['c']} active={r['a']} versions={r['v']}")
PY
docker cp /tmp/before.py "$CM":/tmp/before.py >/dev/null
docker exec "$CM" python /tmp/before.py

echo
echo "=== 3. reset the stuck walk position, then run the REAL celery task ==="
# last_usage_key points past the end from the broken runs, so a resumed run
# would send nothing. Clearing it is what an operator would do; the fix stops it
# recurring.
cat > /tmp/run.py <<'PY'
import os
from coursemate_platform.models import CourseIndexState
from coursemate_platform.tasks.bootstrap import bootstrap_course

cid = os.environ["COURSE"]
st = CourseIndexState.for_course(cid)
st.last_usage_key = ""; st.run_id = ""; st.blocks_indexed = 0
st.save()
print("  cleared walk position; enqueueing bootstrap_course")
r = bootstrap_course.delay(cid)
print("  task id:", r.id)
PY
docker cp /tmp/run.py "$CMS":/tmp/run.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production -e COURSE="$COURSE" "$CMS" \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/run.py').read())" 2>&1 | grep -E "cleared|task id"

echo "  waiting 75s for it to run..."
sleep 75

echo
echo "=== 4. state AFTER — did it SWAP? ==="
docker cp /tmp/before.py "$CM":/tmp/after.py >/dev/null
docker exec "$CM" python /tmp/after.py

echo
echo "=== 5. verdict ==="
cat > /tmp/verdict.py <<'PY'
import os
from coursemate_service.knowledge import get_store
cid = os.environ["COURSE"]
s = get_store()
row = s._conn.execute(
    "SELECT COUNT(*) c, SUM(active) a, COUNT(DISTINCT version) v FROM chunks "
    "WHERE offering_id=?", (cid,)).fetchone()
c, a, v = row["c"], row["a"] or 0, row["v"]
print(f"   chunks={c} active={a} versions={v}")
ok = (v == 1 and c == a and a > 0)
print("   RESULT:", "PASS — one version, all active, swap fired"
      if ok else "FAIL — the enqueued path still is not swapping")
raise SystemExit(0 if ok else 1)
PY
docker cp /tmp/verdict.py "$CM":/tmp/verdict.py >/dev/null
docker exec -e COURSE="$COURSE" "$CM" python /tmp/verdict.py
