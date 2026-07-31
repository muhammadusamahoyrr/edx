#!/usr/bin/env bash
set -eu
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms migrate coursemate_platform 2>&1 | tail -6
cat > /tmp/ct.py <<'PY'
from django.db import connection
with connection.cursor() as c:
    c.execute("SHOW TABLES LIKE 'coursemate%%'")
    print("tables now:", [r[0] for r in c.fetchall()])
PY
docker cp /tmp/ct.py tutor_local-cms-1:/tmp/ct.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/ct.py').read())" 2>&1 | tail -2
