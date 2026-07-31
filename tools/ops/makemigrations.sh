#!/usr/bin/env bash
# Generate the plugin's migrations inside the CMS, where the real Django and the
# real app registry live, then copy them back into the repo.
set -eu
C=tutor_local-cms-1
SITE=/openedx/venv/lib/python3.11/site-packages
docker exec "$C" mkdir -p "$SITE/coursemate_platform/migrations"
docker exec "$C" touch "$SITE/coursemate_platform/migrations/__init__.py"
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production "$C" \
  python /openedx/edx-platform/manage.py cms makemigrations coursemate_platform
docker cp "$C:$SITE/coursemate_platform/migrations" /tmp/cm-migrations
ls -la /tmp/cm-migrations
