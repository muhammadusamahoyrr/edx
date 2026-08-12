"""Write the service's OpenAPI spec to docs/openapi.json — or check it is current.

    make openapi          # regenerate
    make openapi-check    # fail if the committed spec is stale (runs in `make check`)

**Generated, never hand-written.** A hand-maintained spec is a second source of
truth that drifts from the routes on the first busy week, and the drift is
invisible until an integrator builds against it. This reads FastAPI's own schema,
so the spec is wrong only if the code is.

**"Generated" was not enough on its own.** The spec still went stale, because
generating it was a step someone had to remember: Phase 2 added `content_sha256`
and `clo_confidence` to the contracts and nobody re-ran `make openapi`, so the
committed document described an API that no longer existed and nothing said so.
`--check` closes that by making the omission fail the build rather than sit in the
repo — the same reasoning behind `.importlinter`, applied to the API surface.

Both modes serialise through one function, so the writer and the checker cannot
disagree about formatting and report a difference that is only whitespace.

It is committed rather than served, deliberately. `/docs` would expose the whole
API surface — including the service-credential ingest and pack-loading routes —
to anyone who can reach the container, and §3.4 keeps those credential classes
apart precisely so a student-path caller cannot enumerate them.
"""

from __future__ import annotations

import argparse
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


def _where(path: Path) -> str:
    """Repo-relative if it can be, absolute otherwise.

    `relative_to` RAISES for a path outside the repo rather than returning the
    absolute one, so a bare call crashes the moment the output path is redirected
    — which the drift tests do, and which any operator pointing `--check` at a
    copy would too. An error message is not worth an exception.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def render(spec: dict) -> str:
    """The one serialisation. Both modes go through it.

    Sorted and newline-terminated so a regeneration produces a clean diff rather
    than a reordered blob nobody can review — and so `--check` compares content
    rather than key order, which FastAPI does not promise to keep stable.
    """
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def current_spec() -> dict:
    """FastAPI's own schema for the running route table."""
    from coursemate_service.main import app

    # `app.openapi()` memoises into `app.openapi_schema`; cleared so a second
    # call in the same process (the drift test does exactly that) rebuilds from
    # the routes instead of returning a cached document.
    app.openapi_schema = None
    return app.openapi()


def _summary(spec: dict) -> str:
    paths = spec.get("paths", {})
    lines = [f"{len(paths)} paths"]
    for path in sorted(paths):
        methods = ",".join(sorted(m.upper() for m in paths[path]))
        lines.append(f"  {methods:12s} {path}")
    return "\n".join(lines)


def check() -> int:
    """0 if `docs/openapi.json` matches the routes, 1 if it is stale."""
    generated = render(current_spec())

    if not OUT.exists():
        print(f"MISSING: {_where(OUT)} does not exist.\n"
              f"  Run `make openapi` to create it.", file=sys.stderr)
        return 1

    committed = OUT.read_text(encoding="utf-8")
    if committed == generated:
        print(f"OpenAPI is current — {len(current_spec().get('paths', {}))} paths")
        return 0

    # Say what to do, not just that something is wrong. A check whose message
    # leaves the reader guessing gets disabled the first time it fires on
    # someone else's change.
    print(
        f"STALE: {_where(OUT)} no longer matches the routes and contracts.\n"
        f"\n"
        f"  The committed OpenAPI spec is a description of an API that does not\n"
        f"  exist. An integrator building against it would build against fiction.\n"
        f"\n"
        f"  Fix: run `make openapi` and include the regenerated file in your change.\n",
        file=sys.stderr,
    )

    import difflib

    diff = list(difflib.unified_diff(
        committed.splitlines(keepends=True), generated.splitlines(keepends=True),
        fromfile=f"{OUT.name} (committed)", tofile=f"{OUT.name} (generated)", n=2,
    ))
    # Truncated: a contract rename can move thousands of lines, and a wall of
    # diff buries the one-line answer to "what changed".
    sys.stderr.writelines(diff[:60])
    if len(diff) > 60:
        print(f"\n  ... {len(diff) - 60} more diff lines", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the committed spec is stale")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    spec = current_spec()
    OUT.write_text(render(spec), encoding="utf-8")
    print(f"wrote {_where(OUT)} — {_summary(spec)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
