"""Feature B's quality rubric — design §11.2, §11.3.

§9.0 lets a generated practice question reach a student with **no instructor
gate**, and the argument for that is explicit: gating one student's private study
aid is unworkable and protects nobody. What makes it safe instead is that the
question is labelled, it cites its source, and **it is measured**. For this path,
measurement *is* the control.

So this file is not a nice-to-have. It is the other half of a design decision
that has already been made, and if it does not run, the argument for the no-gate
design does not hold.

**Three deterministic checks, and nothing dressed up as more than it is.**

    1. CLO alignment       — is the question tagged to the outcome that was asked for?
    2. Band plausibility   — are marks and difficulty inside the ranges the real bank uses?
    3. Near-duplicate      — did it just reprint a past-paper question and call it new?

All three are token/metadata arithmetic, not judgement. That is the point: they
are cheap, deterministic, and they never claim to measure whether a question is
*good*. Full hallucination detection and the two-rater human study are deferred
(§11.2), and this file says so rather than implying coverage it does not have.

Check 3 uses the same token-overlap floor `ai/verify.py` uses, and inherits the
same honest caveat: token overlap detects reprinting, not paraphrase. A question
reworded from a past paper passes. That is a real gap, named here rather than
discovered later.

    python eval/feature_b_rubric.py generated.json --bank pack.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Same tokenisation rule as `knowledge/store.py`, deliberately. A duplicate
#: detector that tokenised differently from the retriever would disagree with it
#: about what two texts have in common.
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "it", "this",
    "that", "these", "those", "you", "your", "can", "will", "may", "at", "by",
    "from", "not", "no", "do", "does", "did", "have", "has", "had", "which",
    "when", "what", "how", "why", "who", "where", "i", "my", "me", "we", "us",
    "about", "into", "used", "use", "there", "their", "them", "they",
})

#: Jaccard similarity at or above this counts as a near-duplicate. Chosen to sit
#: well clear of two questions that merely share a topic — those land around
#: 0.2–0.4 on this corpus — while catching a reprint with light edits. It is a
#: starting point calibrated on one course, not a settled number, and it errs
#: toward flagging: a false "this is a duplicate" is checkable by a human, and a
#: missed one ships a past paper to a student as though we wrote it.
DUPLICATE_THRESHOLD = 0.6

#: Bloom-style command verbs, by band.
#:
#: **This is the design's own difficulty signal, not an invention.** §7.6 and the
#: design table both derive `difficulty` "from marks + command verb (Bloom level)
#: + model estimate". Using the verb to check the band therefore tests the
#: generated question against the same property the bank's own difficulty was
#: built from.
#:
#: Deterministic and explainable on purpose. An LLM judge would be more
#: sensitive, and it would also drift: a judge that changes cannot tell you
#: whether your generator changed or the judge did (§11.1). Nothing in the spec
#: asks for one here.
#:
#: A question whose verb is in none of these sets scores "not run" rather than
#: passing — the same rule every other check in this file follows.
_BLOOM_VERBS: dict[str, frozenset[str]] = {
    "easy": frozenset({
        "state", "name", "list", "define", "give", "identify", "label", "recall",
        "who", "what", "when", "where",
    }),
    "medium": frozenset({
        "explain", "describe", "compare", "contrast", "summarise", "summarize",
        "illustrate", "apply", "calculate", "outline", "distinguish", "show",
    }),
    "hard": frozenset({
        "evaluate", "assess", "critically", "design", "analyse", "analyze",
        "justify", "propose", "critique", "argue", "devise", "recommend",
    }),
}

#: Below this many distinct outcomes the alignment check cannot discriminate —
#: with one vocabulary the requested outcome is trivially the closest.
_MIN_CLOS_FOR_ALIGNMENT = 2

#: Below this many marked (or difficulty-scored) questions, the bank cannot
#: describe a range and the band check does not run. Two is the minimum at which
#: min and max can differ at all.
_MIN_BAND_SAMPLES = 2


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


#: **Quality** answers "is this a good question?" and reads only the generated
#: text. **Safety** answers "did the pipeline keep its promises?" and reads the
#: metadata the pipeline injected.
#:
#: They are reported apart and never averaged. A safety metric is expected to sit
#: at 1.000 — it is an invariant, so anything else is a defect, not a low score —
#: and mixing it into a quality average would flatter the quality number with a
#: figure that cannot move.
QUALITY_CHECKS = ("clo_alignment", "band_plausibility", "not_a_duplicate")
SAFETY_CHECKS = ("labelled_and_sourced", "metadata_in_range")


@dataclass
class Finding:
    question_index: int
    check: str
    passed: bool
    detail: str


@dataclass
class RubricReport:
    findings: list[Finding] = field(default_factory=list)
    n_questions: int = 0
    #: Present so a caller cannot read a percentage without the sample size —
    #: §11.2's standing rule, and the reason every number here travels with n.
    n_bank: int = 0

    def rate(self, check: str) -> float | None:
        rows = [f for f in self.findings if f.check == check]
        if not rows:
            return None
        return sum(f.passed for f in rows) / len(rows)

    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.passed]

    def n_for(self, check: str) -> int:
        """How many questions this check actually ran on. A rate without its own
        n is unreadable once checks can individually not-run."""
        return sum(1 for f in self.findings if f.check == check)

    def as_dict(self) -> dict:
        return {
            "n_questions": self.n_questions,
            "n_bank": self.n_bank,
            "quality": {c: {"rate": self.rate(c), "n": self.n_for(c)} for c in QUALITY_CHECKS},
            "safety": {c: {"rate": self.rate(c), "n": self.n_for(c)} for c in SAFETY_CHECKS},
            "clo_alignment": self.rate("clo_alignment"),
            "band_plausibility": self.rate("band_plausibility"),
            "not_a_duplicate": self.rate("not_a_duplicate"),
            "labelled_and_sourced": self.rate("labelled_and_sourced"),
            "metadata_in_range": self.rate("metadata_in_range"),
            "failures": [
                {"i": f.question_index, "check": f.check, "detail": f.detail}
                for f in self.failures()
            ],
        }


def clo_vocabularies(bank: list[dict]) -> dict[str, set[str]]:
    """The content vocabulary of each outcome, learned from its own questions.

    Not a hand-written topic list: the bank already says which questions belong
    to which outcome, so their combined terms ARE the outcome's vocabulary. That
    keeps the check course-agnostic and means it cannot drift from the data.
    """
    vocab: dict[str, set[str]] = {}
    for q in bank:
        clo = q.get("clo_id")
        if clo:
            vocab.setdefault(clo, set()).update(tokens(q.get("text", "")))
    return {c: v for c, v in vocab.items() if v}


def command_band(text: str) -> str | None:
    """The band implied by the question's command verb, or None if unrecognised.

    Scans in order and takes the FIRST recognised verb, because a question often
    opens with a scenario — "Given two processes holding one resource each,
    explain why..." — and the operative verb is not always the first word.
    """
    for word in _WORD.findall((text or "").lower()):
        for band, verbs in _BLOOM_VERBS.items():
            if word in verbs:
                return band
    return None


def _bands(bank: list[dict]) -> tuple[tuple[int, int] | None, tuple[float, float] | None]:
    """The ranges the REAL bank uses, derived rather than hardcoded.

    A hardcoded "marks must be 1..20" would be wrong for the first course that
    marks out of 100, and it would be wrong silently — every generated question
    failing a check that describes nothing about this course.

    **Fewer than two data points is not a range**, and returning one anyway is
    the same error in a subtler form: a bank holding a single 100-mark question
    would produce the band (100, 100) and reject every generated question that
    was not worth exactly 100. So the check reports "not run" instead — the same
    rule check 1 follows, and for the same reason: a measurement that cannot be
    made must say so rather than pick a side.
    """
    marks = [q["marks"] for q in bank if q.get("marks") is not None]
    diffs = [q["difficulty"] for q in bank if q.get("difficulty") is not None]
    return (
        (min(marks), max(marks)) if len(marks) >= _MIN_BAND_SAMPLES else None,
        (min(diffs), max(diffs)) if len(diffs) >= _MIN_BAND_SAMPLES else None,
    )


def score(
    generated: list[dict],
    bank: list[dict],
    requested_clo: str | None = None,
    requested_band: str | None = None,
) -> RubricReport:
    """Score generated practice questions against the real past-paper bank.

    **The quality checks read the generated TEXT. The safety checks read the
    metadata.** That split exists because of a measured failure: the first
    version compared `clo_id`, `marks` and `difficulty` on the generated question
    against the source they were copied from, and the generator injects all three
    in code to keep provenance unforgeable. So those checks could not fail — a
    20-question run scored 1.000 on three of four metrics while measuring nothing
    about the questions.

    Two correct decisions collided: enforcing provenance in code (right) made two
    quality checks tautological (unintended). The fix is not to weaken the
    injection but to point the quality checks at the only thing the model
    actually produced — the words.

    Skipped checks report "not run", never a pass.
    """
    report = RubricReport(n_questions=len(generated), n_bank=len(bank))
    mark_band, diff_band = _bands(bank)
    vocab = clo_vocabularies(bank)
    bank_tokens = [(q.get("question_id", "?"), tokens(q.get("text", ""))) for q in bank]

    for i, q in enumerate(generated):
        text = q.get("text", "")

        # --- 1. CLO alignment — QUALITY, reads the text ---------------------
        #
        # Is the generated question closer to the requested outcome's vocabulary
        # than to any OTHER outcome's? Comparing the injected `clo_id` against
        # the requested one can never fail, because the pipeline copies it from
        # the source it selected by that very id.
        #
        # Argmax rather than a threshold: a fixed floor would need calibrating
        # per course, while "nearest outcome" is scale-free and directly answers
        # the question a student cares about — did I get a question about the
        # thing I asked to practise?
        if requested_clo is not None:
            t = tokens(text)
            if len(vocab) < _MIN_CLOS_FOR_ALIGNMENT or requested_clo not in vocab or not t:
                pass  # cannot discriminate -> not run
            else:
                sims = {clo: jaccard(t, v) for clo, v in vocab.items()}
                best = max(sims.values())
                winners = [c for c, v in sims.items() if v == best]
                ok = winners == [requested_clo] and best > 0.0
                nearest = ", ".join(f"{c}={sims[c]:.2f}" for c in sorted(sims, key=sims.get, reverse=True)[:3])
                report.findings.append(Finding(
                    i, "clo_alignment", ok,
                    f"asked for {requested_clo}; nearest by vocabulary: {nearest}",
                ))

        # --- 2. band plausibility — QUALITY, reads the text -----------------
        #
        # Does the command verb the model chose match the band that was asked
        # for? §7.6 derives `difficulty` from "marks + command verb (Bloom
        # level)", so this checks the generated question against the same
        # property the bank's own difficulty was built from — and unlike the
        # metadata comparison below, the model can get it wrong.
        if requested_band is not None:
            got_band = command_band(text)
            if got_band is None:
                pass  # no recognised command verb -> not run
            else:
                report.findings.append(Finding(
                    i, "band_plausibility", got_band == requested_band,
                    f"asked for {requested_band}, command verb reads {got_band}",
                ))

        # --- 2b. metadata in range — SAFETY, reads the injected fields -------
        #
        # **Emitted only when it can actually be evaluated.** The check needs a
        # band from the bank AND a value on the generated question; with either
        # missing there is nothing to compare, and the earlier version recorded a
        # PASS in that situation. Since `PracticeQuestion` carried no `marks` or
        # `difficulty` at all, that meant this metric would have reported 1.00
        # for every generated question ever scored — a number produced entirely
        # by absent data.
        #
        # Omitting the finding is what makes `rate()` return None, which
        # `as_dict()` and the CLI both render as "not run". Same rule check 1
        # already follows for a missing `requested_clo`, applied here too.
        comparable = [
            (name, value, band)
            for name, value, band in (
                ("marks", q.get("marks"), mark_band),
                ("difficulty", q.get("difficulty"), diff_band),
            )
            if value is not None and band is not None
        ]
        if comparable:
            outside = [
                f"{name} {value} outside the bank's {band}"
                for name, value, band in comparable
                if not (band[0] <= value <= band[1])
            ]
            report.findings.append(Finding(
                i, "metadata_in_range", not outside,
                "; ".join(outside) or
                f"within the bank's ranges ({', '.join(n for n, _, _ in comparable)})",
            ))

        # --- 3. near-duplicate --------------------------------------------
        t = tokens(text)
        worst_id, worst = None, 0.0
        for qid, bt in bank_tokens:
            sim = jaccard(t, bt)
            if sim > worst:
                worst_id, worst = qid, sim
        report.findings.append(Finding(
            i, "not_a_duplicate", worst < DUPLICATE_THRESHOLD,
            f"closest bank question {worst_id} at {worst:.2f}",
        ))

        # --- 4. the labelling §9.0 depends on -----------------------------
        # Not a quality check. It is the promise that makes the no-gate design
        # defensible, so a generated question missing it is a design violation,
        # not a low score.
        labelled = bool(q.get("ai_generated")) and bool(q.get("derived_from"))
        report.findings.append(Finding(
            i, "labelled_and_sourced", labelled,
            "ai_generated and derived_from are both required (§9.0)",
        ))

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generated", type=Path, help="JSON list of PracticeQuestion")
    parser.add_argument("--bank", type=Path, required=True, help="JSON list of QuestionRecord")
    parser.add_argument("--clo", default=None, help="the CLO the student asked to practise")
    parser.add_argument("--band", default=None, choices=["easy", "medium", "hard"],
                        help="the difficulty band the student asked for")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    generated = json.loads(args.generated.read_text(encoding="utf-8"))
    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    report = score(generated, bank, args.clo, args.band)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if not report.failures() else 1

    print("=" * 72)
    print(f"FEATURE B RUBRIC — {report.n_questions} generated, bank of {report.n_bank}")
    print("=" * 72)
    print("QUALITY  (reads the generated text — these can fail)")
    for check in QUALITY_CHECKS:
        rate = report.rate(check)
        shown = "not run" if rate is None else f"{rate:.2f}"
        print(f"  {check:22s} {shown:>8s}  n={report.n_for(check)}")
    print("SAFETY   (reads injected metadata — invariants, expected 1.00)")
    for check in SAFETY_CHECKS:
        rate = report.rate(check)
        shown = "not run" if rate is None else f"{rate:.2f}"
        print(f"  {check:22s} {shown:>8s}  n={report.n_for(check)}")
    if report.failures():
        print("\nFAILURES")
        for f in report.failures():
            print(f"  [{f.question_index}] {f.check}: {f.detail}")
    print("\nNOT measured here, and deliberately: whether a question is *good*,")
    print("whether it is factually correct, and paraphrased reuse of a past paper.")
    print("Token overlap detects reprinting, not rewording (§11.2).")
    return 0 if not report.failures() else 1


if __name__ == "__main__":
    sys.exit(main())
