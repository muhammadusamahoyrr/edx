"""Turn a past-paper PDF into an ExamPrepPack, ready for POST /packs/load.

    python tools/extract/extract_pack.py paper.pdf --offering course-v1:X+Y+Z \
        --exam-type final --year 2024 > pack.json

**A tool, not a service module, and that is deliberate.** Extraction is offline
batch work an operator runs once per paper. Putting it in the service would add a
PDF parser to the request path and to the container image for something no
student request ever calls. The output is the same JSON `/packs/load` already
accepts, so nothing new is invented at the boundary.

**Digital text only.** `pypdf` reads the text layer; a scanned paper has none and
produces nothing. OCR and VLM extraction are deferred (§7.6 lists all three
methods, and `extraction_method` records which was used) — this reports an empty
extraction honestly rather than half-guessing.

Chosen `pypdf` over the alternatives: PyMuPDF is faster and more accurate but
AGPL, which is the wrong licence for a project aimed at the Open edX community;
pdfplumber handles layout better but pulls pdfminer.six and Pillow, which is not
worth it while the scope is digital text. `pypdf` is BSD-3 with no required
dependencies.

**What this cannot do, stated because a silent failure here is expensive.** It
has no layout model. A two-column paper interleaves into nonsense, and a question
whose number is rendered as a graphic is invisible. Both show up as a low
`confidence` and a raised `low_confidence_flag` where detectable, and as a wrong
question count where not — which is why the CLI prints what it found and asks the
operator to check it rather than piping straight into the loader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "coursemate-contracts"))

from coursemate_contracts.examprep import (
    ExamPrepPack,
    ExtractionMethod,
    QuestionRecord,
    derive_difficulty,
)

#: A line that starts a new question. Covers "1.", "Q3.", "12)" and the
#: sub-part forms "2(b)" / "2 (b)". Anchored at the start because a bare number
#: mid-sentence is not a question boundary — "worth 10 marks" must not split.
_QUESTION_START = re.compile(
    r"^\s*(?:Q\s*)?(\d{1,2}\s*(?:\([a-z]\))?)\s*[.)]\s+(?=\S)", re.IGNORECASE
)

#: "[10 marks]", "(10 marks)", "10 marks". Printed on the paper, so it is read
#: rather than derived — unlike `difficulty`, which §7.6 requires be labelled
#: derived wherever it appears.
_MARKS = re.compile(r"[\[(]?\s*(\d{1,3})\s*marks?\s*[\])]?", re.IGNORECASE)

#: Lines that are furniture, not content. Dropping them is what keeps a footer
#: from being appended to the last question on every page.
_FURNITURE = re.compile(
    r"^\s*(?:page\s+\d+\s+of\s+\d+|end\s+of\s+(?:paper|examination)|"
    r".*\bcontinued\b\s*$|answer\s+all\s+questions.*|time\s+allowed.*)\s*$",
    re.IGNORECASE,
)


def content_sha256(pdf_bytes: bytes) -> str:
    """Fingerprint of the SOURCE DOCUMENT, not of the extracted pack.

    Hashing the pack would change whenever the parser improved, so re-running a
    better extractor over the same paper would look like a different document and
    silently double the bank. Hashing the bytes answers the question actually
    being asked: have we already imported this paper?
    """
    return hashlib.sha256(pdf_bytes).hexdigest()


def read_pages(pdf_path: Path) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages]


def extract_questions(pages: list[str]) -> list[dict]:
    """Split the text layer into questions, keeping the page each started on.

    A question runs from its number line until the next number line or the end of
    the document, so a wrapped question keeps its continuation — including
    continuations that cross a page break, which is why this walks all pages in
    one pass rather than per page.
    """
    found: list[dict] = []
    current: dict | None = None

    for page_no, text in enumerate(pages, start=1):
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip() or _FURNITURE.match(line):
                continue

            match = _QUESTION_START.match(line)
            if match:
                if current:
                    found.append(current)
                number = re.sub(r"\s+", "", match.group(1))
                current = {
                    "question_number": number,
                    "page": page_no,
                    "lines": [line[match.end():].strip()],
                }
            elif current is not None:
                # A continuation line. Everything before the first question
                # number is a title block and is dropped.
                current["lines"].append(line.strip())

    if current:
        found.append(current)
    return found


#: A line that hands over from the question to the examiner's own answer.
#:
#: Anchored at the start of a line and requiring the separator, so "Answer ALL
#: questions" — the rubric line on the front of every paper — cannot match, and
#: neither can the word "answer" occurring mid-sentence inside a question.
#: Deliberately narrow: a false positive here silently truncates the QUESTION
#: and presents the tail of it to students as the model answer.
_ANSWER_START = re.compile(
    r"^\s*(?:model\s+answer|marking\s+scheme|mark\s+scheme|answer|solution)\s*[:\-—]\s*",
    re.IGNORECASE,
)


def split_reference_answer(lines: list[str]) -> tuple[list[str], list[str]]:
    """Separate a question's own lines from the examiner's answer, if printed.

    Returns `(question_lines, answer_lines)`; the second is empty when the paper
    prints no answer, which is the common case and the only honest default. This
    NEVER writes an answer we composed — it only relocates text the examiner
    already published.
    """
    for i, line in enumerate(lines):
        if _ANSWER_START.match(line or ""):
            head = _ANSWER_START.sub("", line or "", count=1).strip()
            tail = [head] if head else []
            return lines[:i], tail + list(lines[i + 1:])
    return lines, []


def to_record(
    q: dict, *, offering_id: str, tenant: str, source_doc_id: str,
    year: int | None, exam_type: str | None,
) -> QuestionRecord:
    question_lines, answer_lines = split_reference_answer(list(q["lines"]))
    reference_answer = " ".join(p for p in answer_lines if p).strip() or None
    text = " ".join(part for part in question_lines if part).strip()

    # Whether the sentence looked complete is judged BEFORE the marks annotation
    # is removed. Judging it after meant stripping "[10 marks]" also stripped the
    # full stop that ended the sentence, and then penalising the question for not
    # having one — every cleanly extracted question reported 0.8 instead of 1.0.
    # A confidence score that is wrong on the happy path is worse than none: it
    # trains the operator to ignore the column.
    looks_complete = bool(re.search(r"[.?]\s*(?:[\[(]?\s*\d{1,3}\s*marks?\s*[\])]?)?\s*$",
                                    text, re.IGNORECASE))

    marks = None
    hit = _MARKS.search(text)
    if hit:
        marks = int(hit.group(1))
        # Removed from the body: the marks annotation is metadata printed on the
        # paper, and leaving it in the text would feed "[10 marks]" to the
        # generator as if it were part of the question.
        text = _MARKS.sub("", text).strip(" .")

    # Confidence is about the EXTRACTION, never about the question's quality.
    # It drops for the things that actually go wrong on a real paper.
    confidence = 1.0
    if len(text) < 25:
        confidence -= 0.4          # too short to be a whole question
    if marks is None:
        confidence -= 0.2          # most papers print marks; missing is odd
    if not looks_complete:
        confidence -= 0.2          # likely truncated mid-sentence
    confidence = round(max(0.0, confidence), 2)

    return QuestionRecord(
        question_id=f"{source_doc_id}#{q['question_number']}",
        tenant=tenant,
        offering_id=offering_id,
        source_doc_id=source_doc_id,
        page=q["page"],
        question_number=q["question_number"],
        text=text,
        year=year,
        exam_type=exam_type,
        marks=marks,
        # §7.6's derivation: marks + command verb, computed by
        # `derive_difficulty` in contracts so the bands and the thing that feeds
        # them stay in one file. `None` when the question shows neither signal —
        # an unknown difficulty is unknown, never "easy".
        #
        # `difficulty_is_derived` stays True (the model default): the paper did
        # not print this number, we inferred it, and §7.6 requires it be labelled
        # that way wherever it appears.
        difficulty=derive_difficulty(text, marks),
        clo_id=None,               # filled by the offline tagger, never here
        confidence=confidence,
        extraction_method=ExtractionMethod.DIGITAL,
        low_confidence_flag=confidence < 0.8,
        # Only ever the examiner's own words, and `None` when the paper printed
        # none — which is most papers, and this one. Provenance points at the
        # document the answer was actually read from; inline answers share the
        # question's page, and a separate marking scheme would carry its own.
        reference_answer=reference_answer,
        reference_answer_source_doc_id=source_doc_id if reference_answer else None,
        reference_answer_page=q["page"] if reference_answer else None,
    )


def build_pack(
    pdf_path: Path, *, offering_id: str, tenant: str,
    year: int | None, exam_type: str | None,
) -> tuple[ExamPrepPack, dict]:
    pdf_bytes = pdf_path.read_bytes()
    pages = read_pages(pdf_path)
    raw = extract_questions(pages)
    source_doc_id = pdf_path.name

    records = [
        to_record(q, offering_id=offering_id, tenant=tenant,
                  source_doc_id=source_doc_id, year=year, exam_type=exam_type)
        for q in raw
    ]

    pack = ExamPrepPack(
        offering_id=offering_id,
        tenant=tenant,
        clos=[],                   # confirmed by a human at load time (§7.3)
        questions=records,
        content_sha256=content_sha256(pdf_bytes),
    )
    report = {
        "pages": len(pages),
        "empty_pages": sum(1 for p in pages if not p.strip()),
        "questions": len(records),
        "low_confidence": sum(1 for r in records if r.low_confidence_flag),
        "without_marks": sum(1 for r in records if r.marks is None),
    }
    return pack, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--offering", required=True)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--exam-type", default=None,
                        choices=["mid", "final", "quiz", "assignment"])
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help="write here instead of stdout")
    args = parser.parse_args(argv)

    if not args.pdf.exists():
        print(f"no such file: {args.pdf}", file=sys.stderr)
        return 2

    pack, report = build_pack(
        args.pdf, offering_id=args.offering, tenant=args.tenant,
        year=args.year, exam_type=args.exam_type,
    )

    # The report goes to stderr so stdout stays a clean pack for piping, while
    # the operator still sees what was found. A silent extractor is how a paper
    # that yielded three questions out of twelve gets loaded unnoticed.
    print(f"  pages           : {report['pages']} ({report['empty_pages']} with no text layer)",
          file=sys.stderr)
    print(f"  questions found : {report['questions']}", file=sys.stderr)
    print(f"  low confidence  : {report['low_confidence']}", file=sys.stderr)
    print(f"  without marks   : {report['without_marks']}", file=sys.stderr)
    if report["empty_pages"]:
        print("  NOTE: pages with no text layer are almost certainly scans. This "
              "tool reads digital text only; OCR is not implemented.", file=sys.stderr)
    if not report["questions"]:
        print("  NOTHING EXTRACTED — check the paper's numbering style before "
              "loading.", file=sys.stderr)
        return 1
    print("  CHECK THESE BEFORE LOADING. Questions are untagged; run the CLO "
          "tagger next.", file=sys.stderr)

    payload = json.dumps(pack.model_dump(mode="json", exclude_none=False), indent=1)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"  wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
