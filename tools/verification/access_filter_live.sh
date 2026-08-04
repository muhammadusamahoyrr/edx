#!/usr/bin/env bash
# Prove the block-access filter hides real content on the live index.
#
#   MSYS_NO_PATHCONV=1 tools/verification/access_filter_live.sh
set -eu

CM=tutor_local-coursemate-1
HERE="$(cd "$(dirname "$0")" && pwd)"

docker cp "$HERE/access_filter_live.py" "$CM":/tmp/access_filter_live.py >/dev/null
docker exec "$CM" python /tmp/access_filter_live.py
