#!/usr/bin/env bash
# Run every architecture probe and capture the output as evidence.
#
#   bash tools/verification/run_all_probes.sh 2>&1 | tee docs/probe_output.txt
#
# Probes run INSIDE the containers, because that is the only place the questions
# can be answered: modulestore is a Python API, not a network service, so "how do
# I read published lesson text" has no answer from outside the platform. That
# constraint is also why the ingest worker is a Celery worker in the Open edX
# deployment rather than part of the CourseMate service (design §3.4 rule 1).

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "########################################################################"
echo "# 1. DEMOX IMPORT"
echo "########################################################################"
# Ships with Tutor; gives a real course with real block types rather than a
# hand-made two-block toy that would make every later probe uninformative.
tutor local do importdemocourse 2>&1 | tail -15

echo
echo "  verifying the import landed:"
tutor local run cms python -c "
import django; django.setup()
from opaque_keys.edx.keys import CourseKey
from xmodule.modulestore.django import modulestore
from xmodule.modulestore import ModuleStoreEnum
key = CourseKey.from_string('course-v1:edX+DemoX+Demo_Course')
store = modulestore()
course = store.get_course(key)
print('  course      :', course.display_name if course else 'NOT FOUND')
with store.branch_setting(ModuleStoreEnum.Branch.published_only, key):
    blocks = store.get_items(key)
from collections import Counter
counts = Counter(b.scope_ids.block_type for b in blocks)
print('  total blocks:', len(blocks))
for block_type, n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f'    {block_type:14s} {n}')
" 2>&1 | grep -v "^$"

# Probes need to be readable from inside the container.
for probe in probe_02_publish_event.py probe_03_content_storage.py \
             probe_04_apis.py probe_05_06_files_and_xblock.py; do
  docker cp "${HERE}/${probe}" "$(docker ps -qf name=cms | head -1)":/tmp/ 2>/dev/null || true
done

echo
echo "########################################################################"
echo "# 2. XBLOCK_PUBLISHED EVENT FLOW"
echo "########################################################################"
tutor local run cms python /tmp/probe_02_publish_event.py 2>&1

echo
echo "########################################################################"
echo "# 3. CONTENT STORAGE AND MODULESTORE READS"
echo "########################################################################"
tutor local run cms python /tmp/probe_03_content_storage.py 2>&1

echo
echo "########################################################################"
echo "# 4. AUTHENTICATED API CALLS"
echo "########################################################################"
tutor local run lms python /tmp/probe_04_apis.py 2>&1

echo
echo "########################################################################"
echo "# 5 & 6. FILE STORAGE AND XBLOCK INTEGRATION"
echo "########################################################################"
tutor local run cms python /tmp/probe_05_06_files_and_xblock.py 2>&1

echo
echo "All probes complete. Record results in docs/OpenEdX_Verified_Findings.md."
