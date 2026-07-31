#!/usr/bin/env bash
set -eu
cat > /tmp/dump.py <<'PY'
from coursemate_service.api.ingest import get_store
s = get_store()
rows = list(s._conn.execute(
    "SELECT offering_id, COUNT(*) c, SUM(active) a FROM chunks GROUP BY offering_id"))
print("chunks by offering:", [dict(r) for r in rows] or "EMPTY")
print("state:", [dict(x) for x in s._conn.execute("SELECT * FROM offering_state")] or "EMPTY")
PY
docker cp /tmp/dump.py tutor_local-coursemate-1:/tmp/dump.py >/dev/null
docker exec tutor_local-coursemate-1 python /tmp/dump.py
echo "--- db files ---"
docker exec tutor_local-coursemate-1 ls -la /data
