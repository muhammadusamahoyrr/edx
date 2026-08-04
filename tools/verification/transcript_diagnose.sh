#!/usr/bin/env bash
# Surface the real reason get_transcript returns nothing (see the .py).
#
#   MSYS_NO_PATHCONV=1 tools/verification/transcript_diagnose.sh
set -eu

CMS=tutor_local-cms-1
HERE="$(cd "$(dirname "$0")" && pwd)"

docker cp "$HERE/transcript_diagnose.py" "$CMS":/openedx/probes/transcript_diagnose.py >/dev/null
docker exec \
  -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production \
  -e SERVICE_VARIANT=cms \
  "$CMS" python /openedx/probes/transcript_diagnose.py 2>&1
