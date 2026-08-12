"""Generate the exam-paper PDF the extractor tests run against.

Written by hand rather than with reportlab, because a *writer* dependency would
be a second PDF library carried solely to test the first one. The output is a
genuine PDF — `pypdf` reads it the same way it reads a real paper — and it is
regenerated rather than committed as an opaque binary, so what it contains is
reviewable in this file.

    python tools/fixtures/make_exam_pdf.py

What it deliberately includes, because each one is a case the extractor has to
survive on a real paper:

* two pages, so `page` has to be tracked rather than assumed
* question numbers in two styles — "1." and "Q3." — and a sub-part "2(b)"
* a marks annotation in brackets, the usual way a paper prints them
* a header and a footer that are NOT questions and must not become one
* a question whose text wraps across lines, so joining has to be deliberate
"""

from __future__ import annotations

import zlib
from pathlib import Path

PAGES: list[list[str]] = [
    [
        "UNIVERSITY OF EXAMPLE - FINAL EXAMINATION 2024",
        "OEX101: The Open edX Community",
        "Answer ALL questions. Time allowed: 2 hours.",
        "",
        "1. State what the Open edX community is. [2 marks]",
        "",
        "2. Explain how the named release process supports adopters who run",
        "   their own installations. [10 marks]",
        "",
        "2(b). Describe one risk an operator accepts by skipping a release.",
        "[5 marks]",
        "",
        "Page 1 of 2",
    ],
    [
        "OEX101 Final 2024 - continued",
        "",
        "Q3. Critically evaluate the claim that the community's governance is",
        "    its main strength. [15 marks]",
        "",
        "Q4. Name two major members of the Open edX community. [3 marks]",
        "",
        "END OF PAPER",
        "Page 2 of 2",
    ],
]


def _content_stream(lines: list[str]) -> bytes:
    """One page of text at 12pt, 18pt leading, starting near the top."""
    out = ["BT", "/F1 12 Tf", "18 TL", "56 760 Td"]
    for line in lines:
        # Escape the three characters that are syntax inside a PDF string.
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        out.append(f"({safe}) Tj")
        out.append("T*")
    out.append("ET")
    return "\n".join(out).encode("latin-1")


def build() -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # 1-based object number

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    for lines in PAGES:
        raw = _content_stream(lines)
        packed = zlib.compress(raw)
        content_ids.append(add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
            + packed + b"\nendstream"
        ))
        page_ids.append(0)  # placeholder, filled once the Pages id is known

    pages_id = len(objects) + len(PAGES) + 1
    for i, content_id in enumerate(content_ids):
        page_ids[i] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content_id)
        )

    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    actual_pages_id = add(
        b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids)
    )
    assert actual_pages_id == pages_id, "page /Parent references would dangle"
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    # --- serialise with a correct xref table ---------------------------------
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog, xref_at)
    )
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).parent / "oex101_final_2024.pdf"
    target.write_bytes(build())
    print(f"wrote {target} ({target.stat().st_size} bytes)")
