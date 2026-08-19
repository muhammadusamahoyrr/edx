"""Combine several extracted packs into the ONE pack `/packs/load` must receive.

    python tools/extract/merge_packs.py tagged_2022.json tagged_2023.json \
        --clos clos.json -o combined.json

**Why this tool has to exist.** `load_pack` REPLACES an offering's questions —
it deletes every existing row before inserting (`examprep_store.load_pack`).
Loading paper B after paper A does not add B to the bank, it destroys A. So a
multi-paper bank is not built by loading several packs; it is built by merging
several packs and loading once. Anyone who reaches for "just load the second
one" is one command away from a smaller bank than they started with, and the
loader's own counts will report success while it happens.

**Provenance is what keeps ids unique.** `question_id` is `{source_doc_id}#{n}`,
so two papers collide only if they share a filename. That is checked here and
refused, because the loader's own duplicate check would report the ids without
saying which file caused them — and the fix (rename the file, re-extract) is not
guessable from an id list.

**The hash covers the combination, not one document.** `content_sha256` is a
single field describing "the source this pack came from", which a merged pack
does not have. Rather than drop it — which turns the loader's duplicate check off
and silently changes `duplicate_checked` to false — this writes the sha256 of a
manifest of every source: each `(source_doc_id, sha256)` pair, sorted. Adding a
paper later yields a different manifest and therefore a different hash, so the
reload is not mistaken for a duplicate; re-running the identical merge yields the
same hash, so it still is.

**Merging does not compose, and the tool refuses rather than pretends.** Inputs
must each carry exactly one source document — which is all `extract_pack.py`
emits. Feeding a merged pack back in would file every document it contains under
that pack's single manifest hash, so `merge(merge(a,b),c)` and `merge(a,b,c)`
would describe the same bank with different hashes and the loader's duplicate
check would stop working. Merge the single-document packs in one go.

**Nothing is invented.** This tool does not write questions, marks, CLO ids or
text. It concatenates records that came out of `extract_pack.py`, checks them,
and prints what the merge contains so the operator can compare it against the
papers on their desk before loading. A question that is not on a paper does not
enter a bank through here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))

from coursemate_contracts.examprep import CLO, ExamPrepPack  # noqa: E402


class MergeError(Exception):
    """A merge that would have produced a bank nobody asked for."""


def manifest_sha256(sources: list[tuple[str, str | None]]) -> str | None:
    """Fingerprint of the SET of source documents, not of any one of them.

    `None` when any source lacks a hash: a partial manifest would answer "have
    we imported this combination" with a confident wrong answer, and the loader
    already knows how to say "not checked" when told there is no hash.
    """
    if any(digest is None for _, digest in sources):
        return None
    lines = "\n".join(f"{doc}:{digest}" for doc, digest in sorted(sources))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _source_docs(pack: ExamPrepPack) -> set[str]:
    return {q.source_doc_id for q in pack.questions if q.source_doc_id}


def merge(packs: list[ExamPrepPack], *, clos: list[CLO] | None = None) -> ExamPrepPack:
    """One pack carrying every question from every input.

    Order is preserved — inputs in the order given, questions in the order they
    were extracted — so the same inputs always produce the same pack, and a diff
    between two merges is readable.
    """
    if not packs:
        raise MergeError("nothing to merge")

    offerings = {p.offering_id for p in packs}
    if len(offerings) != 1:
        raise MergeError(
            "packs are scoped to different offerings: " + ", ".join(sorted(offerings))
        )
    tenants = {p.tenant for p in packs}
    if len(tenants) != 1:
        raise MergeError("packs are scoped to different tenants: " + ", ".join(sorted(tenants)))

    # **One document per input pack, and this is the check that makes the hash
    # mean anything.** `content_sha256` describes the single source a pack was
    # extracted from, so a pack carrying two documents has one hash for both and
    # the manifest built below would record each of them under it. The result is
    # stable and wrong: merging (a,b) and then (ab,c) produced a different hash
    # from merging (a,b,c), so `already_imported` could not recognise the same
    # bank twice.
    #
    # Refused rather than repaired. Composing merges is the only way to reach
    # this, `extract_pack.py` never emits such a pack, and re-merging from the
    # single-document packs is both easy and what the operator meant.
    for i, pack in enumerate(packs):
        docs = _source_docs(pack)
        if len(docs) > 1:
            raise MergeError(
                f"pack {i + 1} carries {len(docs)} source documents "
                f"({', '.join(sorted(docs))}). Merge does not compose: one hash "
                f"cannot describe several papers. Merge the single-document packs "
                f"from extract_pack.py in one go instead."
            )

    # Refused BEFORE ids are built. Two papers saved under one filename produce
    # colliding `{doc}#{n}` ids, and the loader would reject the merge with a
    # list of ids that does not name the cause.
    seen: dict[str, int] = {}
    for i, pack in enumerate(packs):
        for doc in _source_docs(pack):
            if doc in seen:
                raise MergeError(
                    f"source document {doc!r} appears in pack {seen[doc] + 1} and "
                    f"pack {i + 1}. Two papers cannot share a filename — question "
                    f"ids are built from it. Rename one and re-extract."
                )
            seen[doc] = i

    questions = [q for pack in packs for q in pack.questions]

    # Belt and braces: distinct filenames make this unreachable, but a
    # hand-edited pack can still repeat a number within one document, and the
    # loader's error would arrive after the operator had stopped watching.
    ids = [q.question_id for q in questions]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise MergeError("duplicate question_id(s): " + ", ".join(dupes[:5]))

    # The CLO list is one thing, supplied once, not stitched together from
    # whatever each pack happened to carry. §7.3 makes it the human-confirmed
    # spine; merging two versions of it would produce a spine nobody confirmed.
    if clos is None:
        lists = {json.dumps([c.model_dump() for c in p.clos], sort_keys=True) for p in packs}
        if len(lists) > 1:
            raise MergeError(
                "packs carry different CLO lists — pass --clos to say which one "
                "is the confirmed spine"
            )
        clos = list(packs[0].clos)

    # Safe because every pack carries exactly one document — checked above. Each
    # document is therefore paired with the hash of the bytes it came from, not
    # with a hash describing some collection it was part of.
    sources = sorted({
        (q.source_doc_id, next((p.content_sha256 for p in packs
                                if q.source_doc_id in _source_docs(p)), None))
        for q in questions if q.source_doc_id
    })

    return ExamPrepPack(
        offering_id=packs[0].offering_id,
        tenant=packs[0].tenant,
        clos=clos,
        questions=questions,
        content_sha256=manifest_sha256(sources),
    )


def coverage(pack: ExamPrepPack) -> dict:
    """Per-outcome marks, which is the number that decides what a plan can spend.

    Reported per CLO rather than as one total because a bank with 200 marks in
    two outcomes and none in the third cannot fill a plan that reaches all three
    — and a single total hides exactly that.
    """
    per: dict[str, dict] = defaultdict(
        lambda: {"questions": 0, "marks": 0, "unbudgetable": 0, "sizes": []}
    )
    untagged = 0
    for q in pack.questions:
        if q.clo_id is None:
            untagged += 1
            continue
        row = per[q.clo_id]
        row["questions"] += 1
        if q.marks is None:
            row["unbudgetable"] += 1
        else:
            row["marks"] += q.marks
            row["sizes"].append(q.marks)

    declared = [c.clo_id for c in pack.clos]
    return {
        "per_clo": {c: dict(per[c]) for c in declared},
        "undeclared": {k: dict(v) for k, v in per.items() if k not in declared},
        "untagged": untagged,
        "total_marks": sum(v["marks"] for v in per.values()),
        "documents": sorted({q.source_doc_id for q in pack.questions if q.source_doc_id}),
    }


def report(pack: ExamPrepPack, target: int, out=sys.stderr) -> list[str]:
    """Print what would be loaded, and return the reasons not to load it.

    Warnings rather than a hard failure for the coverage checks: "this bank
    cannot fill a 70-mark plan" is a judgement about whether enough papers have
    been collected, not a malformed pack. The operator decides. A returned list
    that is empty means every check passed.
    """
    cov = coverage(pack)
    problems: list[str] = []

    print(f"  offering        : {pack.offering_id}", file=out)
    print(f"  tenant          : {pack.tenant}", file=out)
    print(f"  documents       : {len(cov['documents'])}", file=out)
    for doc in cov["documents"]:
        n = sum(1 for q in pack.questions if q.source_doc_id == doc)
        print(f"                    {doc}  ({n} questions)", file=out)
    print(f"  questions       : {len(pack.questions)}", file=out)
    print(f"  total marks     : {cov['total_marks']}", file=out)
    print(f"  manifest sha256 : {pack.content_sha256 or 'NONE (duplicate check disabled)'}",
          file=out)
    print(f"  CLOs declared   : {len(pack.clos)}", file=out)

    print("\n  per outcome:", file=out)
    # The share one outcome gets when the budget splits evenly — the fresh
    # student case, where every weight is 1.0. Not a guarantee for a skewed
    # split, and labelled that way rather than presented as a threshold.
    even_share = target / max(1, len(pack.clos))
    for clo in pack.clos:
        row = cov["per_clo"][clo.clo_id]
        sizes = sorted(row["sizes"])
        smallest = sizes[0] if sizes else None
        flag = ""
        if row["questions"] == 0:
            flag = "  <== NO QUESTIONS"
            problems.append(
                f"{clo.clo_id} has no questions: the planner drops it and the "
                f"practice generator abstains for it"
            )
        elif row["marks"] < even_share:
            flag = f"  <== under an even share of {target} ({even_share:.0f})"
            problems.append(
                f"{clo.clo_id} holds {row['marks']} marks, below the {even_share:.0f} "
                f"an even split of {target} would hand it"
            )
        elif smallest is not None and smallest > even_share / 3:
            flag = "  <== coarse: smallest question may not fit a share"
            problems.append(
                f"{clo.clo_id}'s smallest question is {smallest} marks; first-fit "
                f"packing can leave a share largely unspent"
            )
        print(f"    {clo.clo_id:<8} questions={row['questions']:<3} "
              f"marks={row['marks']:<4} sizes={sizes}{flag}", file=out)
        if row["unbudgetable"]:
            print(f"             {row['unbudgetable']} question(s) carry no marks — "
                  f"the planner cannot spend them", file=out)

    if cov["untagged"]:
        print(f"\n  UNTAGGED        : {cov['untagged']} question(s) have no clo_id and are "
              f"invisible to the planner. Run tag_pack.py.", file=out)
        problems.append(f"{cov['untagged']} question(s) untagged")
    if cov["undeclared"]:
        names = ", ".join(sorted(cov["undeclared"]))
        print(f"\n  UNDECLARED CLOs : {names} — tagged on questions but absent from the "
              f"pack's CLO list.", file=out)
        problems.append(f"questions tagged to undeclared outcome(s): {names}")

    low = [q.question_id for q in pack.questions if q.low_confidence_flag]
    if low:
        print(f"\n  LOW CONFIDENCE  : {len(low)} — check these against the paper:", file=out)
        for qid in low[:10]:
            print(f"                    {qid}", file=out)
        problems.append(f"{len(low)} question(s) flagged low confidence")

    return problems


def _read_pack(path: Path) -> ExamPrepPack:
    return ExamPrepPack(**json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packs", type=Path, nargs="+", help="tagged pack JSON files")
    parser.add_argument("--clos", type=Path, default=None,
                        help="JSON list of {clo_id, text, confirmed_by}: the confirmed "
                             "spine. Required when the packs disagree.")
    parser.add_argument("--target-marks", type=int, default=70,
                        help="plan size the bank should be able to fill (default 70)")
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help="write here instead of stdout")
    args = parser.parse_args(argv)

    missing = [p for p in args.packs if not p.exists()]
    if missing:
        print("no such file: " + ", ".join(str(m) for m in missing), file=sys.stderr)
        return 2

    clos = None
    if args.clos:
        clos = [CLO(**c) for c in json.loads(args.clos.read_text(encoding="utf-8"))]

    try:
        merged = merge([_read_pack(p) for p in args.packs], clos=clos)
    except MergeError as exc:
        print(f"  REFUSED: {exc}", file=sys.stderr)
        return 2

    problems = report(merged, args.target_marks)

    if problems:
        print(f"\n  {len(problems)} thing(s) to resolve before loading:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        print("\n  The merge is still written. These are judgements about coverage, "
              "not a malformed pack.", file=sys.stderr)
    else:
        print(f"\n  Coverage supports a {args.target_marks}-mark plan.", file=sys.stderr)

    print("\n  CHECK THIS AGAINST THE PAPERS BEFORE LOADING. `load_pack` REPLACES the "
          "offering's bank — whatever is not in this file will be deleted.", file=sys.stderr)

    payload = json.dumps(merged.model_dump(mode="json"), indent=1)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"  wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    # Non-zero on coverage problems so a pipeline stops rather than loading a
    # bank that cannot fill the plan it was built for.
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
