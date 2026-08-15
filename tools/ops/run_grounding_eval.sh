#!/usr/bin/env bash
# Copy the grounding eval into the service container and run it.
#
#   MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash /mnt/c/.../tools/ops/run_grounding_eval.sh
#
# A file rather than an inline `wsl -- bash -lc '...'`, because inline invocation
# from Git Bash silently drops variable expansion — `SRC="/mnt/c/..."` arrives
# empty, and the failure looks like a missing file rather than a lost variable.
# CLAUDE.md records that; this obeys it.
set -eu

REPO="/mnt/c/Users/The Laptop Hut/Desktop/edx/coursemate"
SCRIPT="$REPO/eval/measure_question_grounding.py"
CONTAINER="tutor_local-coursemate-1"

test -f "$SCRIPT" || { echo "missing: $SCRIPT" >&2; exit 1; }
echo "source: $SCRIPT"

docker cp "$SCRIPT" "$CONTAINER:/tmp/measure_question_grounding.py"
echo "copied into $CONTAINER"

docker exec "$CONTAINER" python /tmp/measure_question_grounding.py "$@"
