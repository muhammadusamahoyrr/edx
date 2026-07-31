#!/usr/bin/env bash
set -eu
cat > /tmp/ct.py <<'PY'
from django.db import connection
with connection.cursor() as c:
    c.execute("SHOW TABLES LIKE 'coursemate%%'")
    print("tables:", c.fetchall() or "NONE")
PY
docker cp /tmp/ct.py tutor_local-cms-1:/tmp/ct.py >/dev/null
docker exec -e DJANGO_SETTINGS_MODULE=cms.envs.tutor.production tutor_local-cms-1 \
  python /openedx/edx-platform/manage.py cms shell -c "exec(open('/tmp/ct.py').read())" 2>&1 | tail -3
echo "--- did a bootstrap ever run? ---"
docker logs tutor_local-cms-worker-1 2>&1 | grep -i "coursemate" | tail -8
