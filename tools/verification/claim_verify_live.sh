#!/usr/bin/env bash
# Does UNSUPPORTED_CLAIM actually get emitted on the running service?
#
#   MSYS_NO_PATHCONV=1 tools/verification/claim_verify_live.sh
#
# The frame has been in the contract and rendered by the browser since v1 with
# nothing sending it. Unit tests now cover the logic; this checks the deployed
# service, because "the code is right" and "the running container has the code"
# are different claims and this project has been caught by the gap before.
#
# Drives the pipeline directly rather than over HTTP: a real request needs a
# minted JWT and a live enrollment check, which would test authentication rather
# than verification.
set -eu

CM=tutor_local-coursemate-1

echo "=== 1. rebuild + redeploy the service ==="
bash "$(cd "$(dirname "$0")/../ops" && pwd)/sync.sh" | tail -1
( cd "$HOME/cm-build" && docker build -q -f deploy/Dockerfile.service -t coursemate/service:0.1.0 . >/dev/null )
docker rm -f "$CM" >/dev/null 2>&1 || true
tutor local start -d coursemate 2>&1 | tail -1
echo -n "  waiting: "
for _ in $(seq 1 30); do
  docker exec "$CM" python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/coursemate/health')" 2>/dev/null && { echo ok; break; }
  echo -n "."; sleep 2
done

echo
echo "=== 2. settings live ==="
docker exec "$CM" python -c "
from coursemate_service.config import settings
print('  verify_claims           :', settings.verify_claims)
print('  claim_support_threshold :', settings.claim_support_threshold)"

echo
echo "=== 3. drive the real pipeline ==="
cat > /tmp/cv.py <<'PY'
import asyncio, time
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import Citation, ChatRequest, FrameType
from coursemate_service.ai import pipeline as pl
from coursemate_service.ai.context import ContextChunk, ContextResult

CTX = ("A deadlock occurs when two processes each hold a lock the other needs "
       "and neither can proceed.")

class Ctx:
    async def fetch(self, q, c):
        return ContextResult(
            chunks=[ContextChunk(text=CTX,
                                 citation=Citation(usage_key="u1", display_name="Locks"),
                                 score=0.9)],
            top_score=0.9)

class Router:
    def __init__(self, text): self.text = text
    async def acompletion(self, **kw):
        text = self.text
        class Chunk:
            def __init__(s):
                s.choices = [type("C", (), {"delta": type("D", (), {"content": text})(),
                                            "finish_reason": "stop"})()]
                s.model = "probe-model"
        async def gen():
            yield Chunk()
        return gen()

def claims():
    now = int(time.time())
    return StudentClaims(sub="u1", course_id="c", offering_id="c", roles=["student"],
                         aud=AUDIENCE_STUDENT, exp=now+300, iat=now)

async def run(answer):
    pl.get_router = lambda: Router(answer)          # noqa: E731
    p = pl.AnswerPipeline(Ctx())
    return [f async for f in p.stream(ChatRequest(question="q"), claims())]

async def main():
    print("  --- answer with one ungrounded sentence ---")
    frames = await run("A deadlock occurs when two processes hold locks. "
                       "Kubernetes schedules replica pods across availability zones.")
    flagged = [f for f in frames if f.type == FrameType.UNSUPPORTED_CLAIM]
    cites   = [f for f in frames if f.type == FrameType.CITATION]
    print(f"    citations={len(cites)}  unsupported={len(flagged)}")
    for f in flagged:
        print(f"    MARKED: {f.text[:70]}")

    print("  --- fully grounded answer (control) ---")
    frames = await run("A deadlock occurs when two processes each hold a lock "
                       "the other needs and neither can proceed.")
    flagged2 = [f for f in frames if f.type == FrameType.UNSUPPORTED_CLAIM]
    print(f"    unsupported={len(flagged2)}")

    ok = len(flagged) == 1 and len(flagged2) == 0
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)

asyncio.run(main())
PY
docker cp /tmp/cv.py "$CM":/tmp/cv.py >/dev/null
docker exec "$CM" python /tmp/cv.py
