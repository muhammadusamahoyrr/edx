#!/usr/bin/env bash
# Second half of the sweep verification: the return path.
#
# Removing content is only half a correctness story. If re-publishing does not
# bring it back, the sweep has traded one silent wrong answer ("cites content
# students cannot see") for another ("cannot answer from content they can").
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
PY
  docker cp /tmp/man.py "$CM":/tmp/man.py >/dev/null
  docker exec "$CM" python /tmp/man.py
}

echo "=== served now (after the sweep removed the unpublished unit) ==="
manifest

echo
echo "=== republish the unit in Studio ==="
cat > /tmp/repub.py <<'PY'
from opaque_keys.edx.keys import UsageKey
from xmodule.modulestore.django import modulestore
from xmodule.modulestore import ModuleStoreEnum

leaf = UsageKey.from_string(open("/tmp/target.txt").read().strip())
store = modulestore()
parent = store.get_parent_location(leaf)
print("publishing unit:", parent)
with store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, parent.course_key):
    store.publish(parent, ModuleStoreEnum.UserID.mgmt_command)
print("published")
PY
docker cp /tmp/repub.py tutor_local-cms-1:/tmp/repub.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/repub.py').read())" 2>&1 | grep -E "publishing|published|Error"

echo
echo "=== wait for the event-driven ingest (no sweep involved) ==="
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 4
  N=$(manifest)
  echo "  t+$((i*4))s served=$N"
  if [ "$N" -ge 221 ]; then echo "  restored by the publish event"; break; fi
done

echo
echo "=== worker log ==="
docker logs --since 2m tutor_local-cms-worker-1 2>&1 | grep -i "coursemate\|reconcile\|sweep" | tail -12
