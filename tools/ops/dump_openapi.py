"""Write the service's OpenAPI spec to docs/openapi.json.

    make openapi

**Generated, never hand-written.** A hand-maintained spec is a second source of
truth that drifts from the routes on the first busy week, and the drift is
invisible until an integrator builds against it. This reads FastAPI's own schema,
so the spec is wrong only if the code is.

It is committed rather than served, deliberately. `/docs` would expose the whole
API surface — including the service-credential ingest and pack-loading routes —
to anyone who can reach the container, and §3.4 keeps those credential classes
apart precisely so a student-path caller cannot enumerate them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "coursemate-service"))
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))

# `Settings` has no defaults for these, on purpose: a deployment must not be able
# to start unconfigured and silently accept unsigned tokens. Obvious placeholders
# so nothing here can be mistaken for a credential.
os.environ.setdefault("COURSEMATE_JWT_SIGNING_KEY", "openapi-dump-not-a-real-secret-32b+")
os.environ.setdefault("COURSEMATE_SERVICE_CREDENTIAL", "openapi-dump-not-a-real-cred-32b+")
os.environ.setdefault("COURSEMATE_INDEX_PATH", ":memory:")
os.environ.setdefault("COURSEMATE_EXAMPREP_PATH", ":memory:")

OUT = ROOT / "docs" / "openapi.json"


def main() -> int:
    from coursemate_service.main import app

    spec = app.openapi()
    # Sorted and newline-terminated so a regeneration produces a clean diff
    # rather than a reordered blob nobody can review.
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    paths = spec.get("paths", {})
    print(f"wrote {OUT.relative_to(ROOT)} — {len(paths)} paths")
    for path in sorted(paths):
        methods = ",".join(sorted(m.upper() for m in paths[path]))
        print(f"  {methods:12s} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
