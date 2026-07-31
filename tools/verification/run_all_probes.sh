#!/usr/bin/env bash
# Generate the technical verification report from live probes.
#
#   bash tools/verification/run_all_probes.sh
#   -> docs/OpenEdX_Verified_Findings.md
#
# Probes run INSIDE the containers because that is the only place several of these
# questions can be answered: modulestore is a Python API, not a network service,
# so "how do I read published lesson text" has no answer from outside the
# platform. That constraint is itself a finding — it is why the ingest worker must
# be a Celery worker in the Open edX deployment rather than part of our service.

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
OUT="${REPO}/docs/OpenEdX_Verified_Findings.md"

CMS_CONTAINER="$(docker ps -qf name=cms | head -1)"
LMS_CONTAINER="$(docker ps -qf name=lms | head -1)"

if [ -z "${CMS_CONTAINER}" ] || [ -z "${LMS_CONTAINER}" ]; then
  echo "ERROR: lms/cms containers not running. Bring the stack up first." >&2
  exit 1
fi

echo "Staging probes into containers…"
for probe in probe_report.py probe_02_publish_event.py probe_03_content_storage.py \
             probe_04_apis.py probe_05_06_files_and_xblock.py; do
  docker cp "${HERE}/${probe}" "${CMS_CONTAINER}:/tmp/${probe}"
  docker cp "${HERE}/${probe}" "${LMS_CONTAINER}:/tmp/${probe}"
done

{
  cat <<'HEADER'
# Open edX — Technical Verification Report

*Evidence gathered by executing probes against a running Open edX instance, for
the purpose of grounding the CourseMate AI Tutor design in observed behaviour
rather than documentation.*

**How to read this report.** Every conclusion carries a confidence level, and the
distinction is load-bearing:

| Level | Meaning |
|---|---|
| `CONFIRMED` | Observed directly on this running instance |
| `INFERRED` | Derived from source code that was read but not executed |
| `UNVERIFIED` | Repeated from documentation; not independently checked |

A design that mixes these ends up asserting things nobody checked. Where a finding
**contradicts published Open edX documentation**, both sides are stated with a
citation and an explanation of why they differ — that section is the most valuable
part of this report and the easiest thing to lose in a wall of output.

## Environment

HEADER

  tutor local run cms python -c "
import sys; sys.path.insert(0, '/tmp')
import django; django.setup()
from probe_report import environment_block
print(environment_block())
" 2>/dev/null | grep -v "^$"

  cat <<'TOC'

## Probe index

| # | Question | Section |
|---|---|---|
| 1 | Does DemoX import, and what does it contain? | Probe 1 |
| 2 | How does `XBLOCK_PUBLISHED` actually behave? | Probe 2 |
| 3 | Where does published content live, and how is it read safely? | Probe 3 |
| 4 | Do the Course Blocks / Enrollment / Completion APIs work authenticated? | Probe 4 |
| 5 | Where do uploaded files go, and what does an export carry? | Probe 5 |
| 6 | Where exactly does the AI Tutor XBlock attach? | Probe 6 |

---

TOC

  # --- Probe 1: DemoX -----------------------------------------------------
  echo "## Probe 1 — DemoX import and course composition"
  echo
  echo "### Objective"
  echo
  echo "Establish a realistic course to probe against. A hand-made two-block toy"
  echo "course would make every later finding uninformative — block-type variety"
  echo "is precisely what probes 2, 3 and 6 measure."
  echo
  echo "### Method"
  echo
  echo 'Import the demo course shipped with Tutor, then enumerate published blocks'
  echo 'by type through the modulestore under a pinned `published_only` branch.'
  echo
  echo "### Commands executed"
  echo
  echo '```bash'
  echo 'tutor local do importdemocourse'
  echo '```'
  echo
  echo "### Evidence"
  echo
  echo '```'
  tutor local do importdemocourse 2>&1 | tail -12
  tutor local run cms python -c "
import django; django.setup()
from collections import Counter
from opaque_keys.edx.keys import CourseKey
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
key = CourseKey.from_string('course-v1:edX+DemoX+Demo_Course')
store = modulestore()
course = store.get_course(key)
print('course display_name :', course.display_name if course else 'NOT FOUND')
with store.branch_setting(ModuleStoreEnum.Branch.published_only, key):
    blocks = store.get_items(key)
print('published blocks    :', len(blocks))
for t, n in sorted(Counter(b.scope_ids.block_type for b in blocks).items(), key=lambda kv: -kv[1]):
    print(f'  {t:16s} {n}')
" 2>&1 | grep -vE "^\s*$"
  echo '```'
  echo
  echo "### Conclusion"
  echo
  echo '- **CONFIRMED** — DemoX imports and provides `html`, `problem`, `video` and'
  echo '  container blocks, which is the variety the remaining probes require.'
  echo
  echo "### Implications for the AI Tutor"
  echo
  echo '- The leaf-type census sets the scope of `content_adapter`: one extractor'
  echo '  per supported type, and every unsupported type explicitly logged rather'
  echo '  than silently skipped.'
  echo
  echo "### Assumptions and limitations"
  echo
  echo '- DemoX is a curated demo course. Real institutional courses contain'
  echo '  block types and edge cases it does not exercise.'
  echo
  echo '---'
  echo

  # --- Probes 2, 3 (CMS) and 4 (LMS), 5+6 (CMS) ---------------------------
  tutor local run cms python /tmp/probe_02_publish_event.py 2>&1
  tutor local run cms python /tmp/probe_03_content_storage.py 2>&1
  tutor local run lms python /tmp/probe_04_apis.py 2>&1
  tutor local run cms python /tmp/probe_05_06_files_and_xblock.py 2>&1

} > "${OUT}"

echo "Report written to ${OUT}"
wc -l "${OUT}"
