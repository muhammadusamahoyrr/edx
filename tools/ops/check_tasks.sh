#!/usr/bin/env bash
# Are our Celery tasks registered in the workers that would actually run them?
#
# **Reads each worker's own startup banner, not a `manage.py shell`.** The earlier
# version asked `current_app.tasks` inside a shell and printed
# "coursemate tasks: NONE" while those same workers were executing our tasks
# perfectly well — a shell is a different process, and what autodiscovery finds
# there does not reflect what the running worker loaded.
#
# A check that reports failure on a healthy system is worse than no check: it
# trains you to ignore it, and the real failure it exists to catch — a missing
# dist-info, so the app never reaches INSTALLED_APPS and every enqueued task dies
# with "Received unregistered task" while Publish still returns 200 — looks
# identical from the outside.
#
# `celery inspect` is avoided for the same reason: it round-trips over the broker
# and returns nothing while a worker is still booting, which is a timing artifact
# that reads exactly like a missing task.
set -eu

EXPECTED="bootstrap_course delete_block ingest_published_block reconcile_all reconcile_course"
STATUS=0

for c in tutor_local-cms-worker-1 tutor_local-lms-worker-1; do
  echo "--- $c ---"
  echo "  started: $(docker inspect "$c" --format '{{.State.StartedAt}}')"

  BANNER=$(docker logs "$c" 2>&1 | grep -oE "coursemate_platform\.tasks\.[a-z_]+\.[a-z_]+" | sort -u || true)
  if [ -z "$BANNER" ]; then
    echo "  NO coursemate tasks in the startup banner."
    echo "  Most likely: no dist-info in this container, so the app never reached"
    echo "  INSTALLED_APPS and Celery autodiscovered nothing. Check check_install.sh."
    STATUS=1
    continue
  fi
  echo "$BANNER" | sed 's/^/    . /'

  for t in $EXPECTED; do
    echo "$BANNER" | grep -q "\.${t}\$" || { echo "  MISSING: $t"; STATUS=1; }
  done
done

echo
if [ "$STATUS" -eq 0 ]; then
  echo "All expected tasks registered in both workers."
  echo "NOTE: registration is not execution. Enqueue a real task to settle that —"
  echo "      the sweep probes do exactly that."
else
  echo "FAILED — see above."
fi
exit "$STATUS"
