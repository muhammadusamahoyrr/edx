"""Tag an extracted pack with course learning outcomes, offline.

    python tools/extract/tag_pack.py pack.json --clos clos.json -o tagged.json

Sits between `extract_pack.py` and `POST /packs/load`: the extractor deliberately
leaves `clo_id` unset, and this fills it in where it can. Nothing student-facing
runs it — it is operator batch work on the `cheap` deployment.

The CLO list is supplied rather than invented. §7.3 makes outcome extraction
*assisted, never asserted*: a human confirms the list before it becomes the
spine, and this tool tags against that confirmed list. It will not tag anything
if the list is empty, because without a vocabulary there is no proposal to make.

Re-runnable on purpose. Only questions with no `clo_id` are considered, so
running it again after a provider outage retries exactly the failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "coursemate-service"))
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="pack JSON from extract_pack.py")
    parser.add_argument("--clos", type=Path, default=None,
                        help="JSON list of {clo_id, text, confirmed_by}; omit if "
                             "the pack already carries its CLOs")
    parser.add_argument("-o", "--out", type=Path, default=None)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)

    from coursemate_contracts.examprep import CLO, ExamPrepPack
    from coursemate_service.ai.clo_tagger import tag_pack

    pack = ExamPrepPack(**json.loads(args.pack.read_text(encoding="utf-8")))
    if args.clos:
        pack = pack.model_copy(update={
            "clos": [CLO(**c) for c in json.loads(args.clos.read_text(encoding="utf-8"))]
        })

    if not pack.clos:
        print("  no CLOs supplied — nothing can be tagged. Pass --clos.", file=sys.stderr)
        return 2

    tagged, report = asyncio.run(tag_pack(pack, max_attempts=args.max_attempts))

    # stderr, so stdout stays a clean pack for piping into the loader.
    r = report.as_dict()
    print(f"  total          : {r['total']}", file=sys.stderr)
    print(f"  already tagged : {r['already_tagged']}", file=sys.stderr)
    print(f"  tagged         : {r['tagged']}", file=sys.stderr)
    print(f"  low confidence : {r['low_confidence']}  (kept, flagged)", file=sys.stderr)
    print(f"  untagged       : {r['untagged']}  (refused — safe, re-runnable)", file=sys.stderr)
    print(f"  failed         : {r['failed']}  (provider — RETRY by re-running)",
          file=sys.stderr)

    if report.outcomes:
        print("\n  why questions were not tagged:", file=sys.stderr)
        seen: dict[str, int] = {}
        for o in report.outcomes:
            if o.status != "tagged":
                seen[o.reason] = seen.get(o.reason, 0) + 1
        for reason, n in sorted(seen.items(), key=lambda kv: -kv[1]) or [("none", 0)]:
            if n:
                print(f"    {n:>3d}  {reason}", file=sys.stderr)

    if r["failed"]:
        print("\n  Re-run this command to retry the failures. Tags already applied "
              "are not touched.", file=sys.stderr)

    payload = json.dumps(tagged.model_dump(mode="json"), indent=1)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"  wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    # Non-zero only for provider failures: a refusal is a correct outcome and
    # must not fail a pipeline.
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
