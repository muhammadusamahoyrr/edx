#!/usr/bin/env bash
# Does a minted token verify end to end on the RUNNING service?
#
#   MSYS_NO_PATHCONV=1 tools/verification/auth_probe.sh
#
# `/whoami` exists for exactly this and had no caller anywhere in the repo — a
# debugging aid nobody could reach when they needed it. It answers in
# milliseconds and returns the claims the service actually decoded, which
# separates three failures that look identical from a browser:
#
#   * the token is wrong           -> 401 here
#   * the token is fine, the STREAM is broken -> 200 here, hang there
#   * the token is fine and the student is not enrolled -> 200 here, abstain there
#
# Phase D2 spent an hour on the third case. `/whoami` would have settled it in
# one call, because it verifies the token WITHOUT touching retrieval, the
# boundary, or a model.
#
# Read-only: it mints a short-lived token and reads it back. Nothing is written.
set -eu

CM=tutor_local-coursemate-1
OFFERING="${1:-course-v1:OpenedX+DemoX+DemoCourse}"
SUB="${2:-5}"

echo "=== 1. is the service answering at all? ==="
docker exec "$CM" python -c "
import urllib.request
print('  health:', urllib.request.urlopen('http://127.0.0.1:8000/coursemate/health', timeout=5).status)
"

echo
echo "=== 2. mint a token the way the XBlock does, then read it back ==="
docker exec -i -e OFFERING="$OFFERING" -e SUB="$SUB" "$CM" python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

import jwt
from coursemate_contracts.auth import AUDIENCE_STUDENT
from coursemate_service.config import settings

OFFERING = os.environ["OFFERING"]
SUB = os.environ["SUB"]
now = int(time.time())

token = jwt.encode(
    {
        "sub": SUB, "username": "probe", "course_id": OFFERING,
        "offering_id": OFFERING, "roles": ["student"],
        "aud": AUDIENCE_STUDENT, "iss": "coursemate-xblock",
        "exp": now + 120, "iat": now,
    },
    settings.jwt_signing_key,
    algorithm="HS256",
)

req = urllib.request.Request(
    "http://127.0.0.1:8000/coursemate/api/whoami",
    headers={"Authorization": "Bearer " + token},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        claims = json.load(r)
    print(f"  {r.status} — the service decoded this token")
    for field in ("sub", "username", "offering_id", "roles", "aud", "group_tokens"):
        if field in claims:
            print(f"    {field:14s} {claims[field]}")
    print(f"    expires in    {claims['exp'] - now}s")
except urllib.error.HTTPError as exc:
    print(f"  {exc.code} — token REJECTED: {exc.read().decode()[:200]}")
    raise SystemExit(1)
PY

echo
echo "=== 3. a token signed with the wrong key must be refused ==="
docker exec -i -e OFFERING="$OFFERING" "$CM" python - <<'PY'
import os
import time
import urllib.error
import urllib.request

import jwt
from coursemate_contracts.auth import AUDIENCE_STUDENT

now = int(time.time())
bad = jwt.encode(
    {
        "sub": "1", "course_id": os.environ["OFFERING"],
        "offering_id": os.environ["OFFERING"], "roles": ["student"],
        "aud": AUDIENCE_STUDENT, "exp": now + 120, "iat": now,
    },
    "not-the-signing-key-but-long-enough-to-sign",
    algorithm="HS256",
)
req = urllib.request.Request(
    "http://127.0.0.1:8000/coursemate/api/whoami",
    headers={"Authorization": "Bearer " + bad},
)
try:
    urllib.request.urlopen(req, timeout=10)
    print("  *** ACCEPTED a token we did not sign — investigate immediately ***")
    raise SystemExit(1)
except urllib.error.HTTPError as exc:
    if exc.code == 401:
        print("  401 — forged token refused, as it must be")
    else:
        print(f"  unexpected {exc.code}")
        raise SystemExit(1)
PY

echo
echo "auth verified: a real token is decoded, a forged one is refused."
