#!/usr/bin/env bash
set -eu
cat > /tmp/ia.py <<'PY'
from django.conf import settings
print("matching INSTALLED_APPS entries:",
      [a for a in settings.INSTALLED_APPS if "coursemate" in a.lower()] or "NONE")
from django.apps import apps
print("app registry:", [c.name for c in apps.get_app_configs() if "coursemate" in c.name.lower()] or "NONE")
from celery import current_app
print("autodiscover finalized:", current_app.finalized)
print("coursemate tasks:", sorted(n for n in current_app.tasks if "coursemate" in n) or "NONE")
PY
docker cp /tmp/ia.py tutor_local-cms-worker-1:/tmp/ia.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-worker-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/ia.py').read())" 2>&1 | tail -5
