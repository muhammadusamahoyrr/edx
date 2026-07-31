#!/usr/bin/env bash
# Reconciliation sweep, end to end against the live stack (§5.4).
#
# The claim under test is not "the code runs". It is: after an instructor
# unpublishes a unit, that unit stops being retrievable — even though Open edX
# emits NO event to tell us it happened. That is the one live correctness gap
# LIMITATIONS.md §5 named, so it is verified against the real modulestore and
# the real index, not a fixture.
set -eu

COURSE="course-v1:OpenedX+DemoX+DemoCourse"
CM=tutor_local-coursemate-1
CRED=$(grep COURSEMATE_SERVICE_CREDENTIAL "$HOME/.local/share/tutor/config.yml" | awk '{print $2}' | tr -d '"')

manifest() {
  cat > /tmp/man.py <<PY
import json, urllib.request
req = urllib.request.Request(
    "http://localhost:8000/coursemate/api/ingest/manifest/$COURSE",
    headers={"Authorization": "Bearer $CRED"})
d = json.load(urllib.request.urlopen(req))
print(d["count"])
open("/tmp/keys.txt", "w").write("\n".join(sorted(d["usage_keys"])))
PY
  docker cp /tmp/man.py "$CM":/tmp/man.py >/dev/null
  docker exec "$CM" python /tmp/man.py
}

echo "=== 1. baseline: blocks currently SERVED ==="
BEFORE=$(manifest)
echo "served: $BEFORE"
docker cp "$CM":/tmp/keys.txt /tmp/before.txt >/dev/null

echo
echo "=== 2. pick a published html leaf and UNPUBLISH it in Studio ==="
cat > /tmp/unpub.py <<PY
from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule.modulestore.django import modulestore
from xmodule.modulestore import ModuleStoreEnum

target = open("/tmp/target.txt").read().strip()
key = UsageKey.from_string(target)
store = modulestore()
# Unpublish the PARENT vertical: Studio unpublishes at unit granularity, and a
# leaf's visibility follows its unit. Unpublishing the leaf alone would test a
# path instructors cannot actually take.
parent = store.get_parent_location(key)
print("unpublishing unit:", parent)
store.unpublish(parent, ModuleStoreEnum.UserID.mgmt_command)
print("done")
PY

# Choose a target from what is actually served, so the test cannot pass by
# unpublishing something that was never indexed.
head -1 /tmp/before.txt > /tmp/target.txt
TARGET=$(cat /tmp/target.txt)
echo "target block: $TARGET"

docker cp /tmp/unpub.py tutor_local-cms-1:/tmp/unpub.py >/dev/null
docker cp /tmp/target.txt tutor_local-cms-1:/tmp/target.txt >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/unpub.py').read())"

echo
echo "=== 3. NOTHING happened automatically — no unpublish event exists ==="
sleep 3
AFTER_EVENT=$(manifest)
echo "served after unpublish, before sweep: $AFTER_EVENT"

echo
echo "=== 4. run the sweep ==="
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms coursemate_reconcile --course "$COURSE" --inline

echo
echo "=== 5. served after the sweep ==="
AFTER=$(manifest)
echo "served: $AFTER"
docker cp "$CM":/tmp/keys.txt /tmp/after.txt >/dev/null
echo "removed:"
comm -23 /tmp/before.txt /tmp/after.txt | sed 's/^/  /'
