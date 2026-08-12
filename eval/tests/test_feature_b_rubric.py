"""The rubric is a control, so it gets tested like one.

§9.0 permits a generated practice question to reach a student with no instructor
gate *because* it is measured. A rubric that silently passes everything would
leave that argument standing on nothing — which is worse than having no rubric,
because the design would still be claiming the protection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feature_b_rubric import DUPLICATE_THRESHOLD, jaccard, score, tokens

BANK = [
    {"question_id": "P1", "clo_id": "CLO-1", "marks": 10, "difficulty": 0.6,
     "text": "Explain how a deadlock arises between two processes."},
    {"question_id": "P1b", "clo_id": "CLO-1", "marks": 12, "difficulty": 0.6,
     "text": "Explain circular wait and resource holding in a deadlock."},
    {"question_id": "P2", "clo_id": "CLO-2", "marks": 5, "difficulty": 0.3,
     "text": "Describe the round robin scheduling algorithm."},
    {"question_id": "P2b", "clo_id": "CLO-2", "marks": 6, "difficulty": 0.3,
     "text": "Describe how a scheduling time quantum affects turnaround."},
]

#: On-topic for CLO-1 and NOT a reprint of either CLO-1 source. The default has
#: to satisfy the quality checks so a failure in a test means what it says.
_ON_TOPIC_CLO1 = "Explain why a deadlock cannot resolve once processes hold and wait."


def gen(**kw) -> dict:
    base = {"text": _ON_TOPIC_CLO1,
            "clo_id": "CLO-1", "ai_generated": True, "derived_from": ["P1"]}
    return {**base, **kw}


def rate(report, check):
    return report.rate(check)


# --- check 1: CLO alignment ------------------------------------------------


def test_a_question_about_the_wrong_topic_fails():
    """The concrete harm: a student asks to practise deadlock and is handed a
    scheduling question. Note the METADATA is perfect — `clo_id` says CLO-1 —
    and the check still fails, because it reads the words."""
    off_topic = gen(text="Describe how the round robin scheduling quantum is chosen.")
    assert rate(score([off_topic], BANK, requested_clo="CLO-1"), "clo_alignment") == 0.0


def test_an_on_topic_question_passes():
    assert rate(score([gen()], BANK, requested_clo="CLO-1"), "clo_alignment") == 1.0


def test_the_check_reports_not_run_rather_than_passing():
    """A check that silently passes when it cannot run is worse than one that
    says it did not run — the first is indistinguishable from success."""
    assert rate(score([gen()], BANK), "clo_alignment") is None


# --- check 2: band plausibility --------------------------------------------


def test_marks_outside_the_banks_range_fail():
    assert rate(score([gen(marks=500)], BANK), "metadata_in_range") == 0.0


def test_the_bands_come_from_the_bank_not_from_a_constant():
    """A hardcoded 1..20 would be wrong for the first course marked out of 100,
    and wrong silently — every generated question failing a check that describes
    nothing about that course."""
    big = [{"question_id": "P1", "text": "x", "marks": 60, "difficulty": 0.5},
           {"question_id": "P2", "text": "y", "marks": 100, "difficulty": 0.9}]
    assert rate(score([gen(marks=80)], big), "metadata_in_range") == 1.0
    assert rate(score([gen(marks=80)], BANK), "metadata_in_range") == 0.0


def test_a_single_data_point_is_not_a_range():
    """A bank holding one 100-mark question would otherwise produce the band
    (100, 100) and reject every generated question not worth exactly 100. The
    check reports 'not run' rather than picking a side.

    Found by this test, not by review: the first version derived a band from any
    non-empty sample.

    **Updated in the Phase 0.5 contract freeze.** It used to assert 1.0 here,
    which was the vacuous pass itself: with no usable band there is nothing to
    compare against, so the honest result is "not run", not "passed"."""
    one = [{"question_id": "P1", "text": "x", "marks": 100, "difficulty": 0.5}]
    assert rate(score([gen(marks=80)], one), "metadata_in_range") is None


def test_absent_marks_do_not_fail_the_band_check():
    """Not every question carries marks. Missing is not out-of-range, and
    conflating them would flag the whole bank of an unmarked paper.

    **Updated in the Phase 0.5 contract freeze.** The intent is unchanged — an
    absent value must not FAIL — but the result is now "not run" rather than
    "passed". `gen()` supplies neither marks nor difficulty, which is exactly the
    shape every generated question had before `PracticeQuestion` carried them."""
    assert rate(score([gen(marks=None)], BANK), "metadata_in_range") is None


# --- check 3: near-duplicate -----------------------------------------------


def test_a_reprinted_past_paper_question_is_caught():
    """The failure this exists for: presenting a real exam question to a student
    as though we generated it."""
    report = score([gen(text=BANK[0]["text"])], BANK)
    assert rate(report, "not_a_duplicate") == 0.0
    assert "P1" in report.failures()[0].detail


def test_a_genuinely_new_question_passes():
    assert rate(score([gen()], BANK), "not_a_duplicate") == 1.0


def test_sharing_a_topic_is_not_a_duplicate():
    """Two questions about deadlock are not the same question. A detector that
    could not tell them apart would flag every on-topic question ever generated,
    and a check that always fires is a check that gets switched off."""
    similar = gen(text="Given a resource allocation graph, identify whether the "
                       "system is in a safe state and justify your answer.")
    assert jaccard(tokens(similar["text"]), tokens(BANK[0]["text"])) < DUPLICATE_THRESHOLD
    assert rate(score([similar], BANK), "not_a_duplicate") == 1.0


def test_the_documented_blind_spot_is_real():
    """Token overlap detects reprinting, not rewording. This is asserted rather
    than only mentioned in a docstring, so the limitation stays true as the
    threshold moves — if a future change closes it, this test fails and the
    documentation gets corrected instead of quietly becoming wrong."""
    reworded = gen(text="Between a pair of tasks, describe the circumstances in "
                        "which circular waiting emerges and cannot resolve.")
    assert rate(score([reworded], BANK), "not_a_duplicate") == 1.0


# --- check 4: the labelling §9.0 depends on --------------------------------


def test_an_unlabelled_generated_question_fails():
    """Not a quality score. §9.0's whole argument for skipping the instructor
    gate is that the output is labelled and cited; drop either and the design
    decision no longer holds."""
    assert rate(score([gen(ai_generated=False)], BANK), "labelled_and_sourced") == 0.0
    assert rate(score([gen(derived_from=[])], BANK), "labelled_and_sourced") == 0.0
    assert rate(score([gen()], BANK), "labelled_and_sourced") == 1.0


# --- reporting -------------------------------------------------------------


def test_the_sample_size_travels_with_the_rates():
    """§11.2's standing rule: never a bare percentage. A caller must not be able
    to read 1.00 without seeing it came from one question."""
    out = score([gen()], BANK, requested_clo="CLO-3").as_dict()
    assert out["n_questions"] == 1
    assert out["n_bank"] == len(BANK)


def test_an_empty_bank_does_not_crash_or_falsely_pass():
    """A course with no past papers loaded. The duplicate check has nothing to
    compare against, and must not report that as 'verified unique'."""
    out = score([gen()], [])
    assert out.rate("not_a_duplicate") == 1.0
    assert out.as_dict()["n_bank"] == 0


# --- contract freeze (Phase 0.5) -------------------------------------------


def test_practice_question_carries_every_field_the_rubric_reads():
    """The drift test. Both halves were written in this repo and had never been
    run together: the rubric read `marks` and `difficulty`, `PracticeQuestion`
    carried neither, and the band check passed on the absence.

    This reads the field names out of the rubric's own source rather than
    hardcoding them, so adding a new `q.get("…")` to `score()` without adding the
    field to the contract fails here instead of silently producing a vacuous
    metric.
    """
    import re
    from pathlib import Path

    from coursemate_contracts.examprep import PracticeQuestion

    src = (Path(__file__).resolve().parent.parent / "feature_b_rubric.py").read_text(
        encoding="utf-8"
    )
    # Scoped to the GENERATED-question loop. The bank comprehension above it
    # also binds `q` and reads `question_id`, which is a QuestionRecord field and
    # has no business on a PracticeQuestion — an unscoped regex reported it as a
    # missing field on the first run of this test.
    start = src.index("for i, q in enumerate(generated):")
    body = src[start:src.index("return report", start)]
    read_by_rubric = set(re.findall(r'q\.get\(\s*"([a-z_]+)"', body))
    assert read_by_rubric, "regex found nothing — `score()` was restructured"

    missing = sorted(read_by_rubric - set(PracticeQuestion.model_fields))
    assert not missing, f"rubric reads fields PracticeQuestion does not have: {missing}"


def test_difficulty_band_is_not_a_stored_field():
    """Derived from `difficulty` by one shared helper. Storing it too would be a
    second source of truth that can disagree with the first."""
    from coursemate_contracts.examprep import PracticeQuestion

    assert "difficulty_band" not in PracticeQuestion.model_fields


def test_a_real_practice_question_scores_without_vacuous_passes():
    """End to end on the actual contract object, not a hand-made dict."""
    from coursemate_contracts.examprep import PracticeQuestion

    pq = PracticeQuestion(
        text=_ON_TOPIC_CLO1, clo_id="CLO-1", derived_from=["P1"],
        marks=8, difficulty=0.5,
    )
    report = score([pq.model_dump()], BANK, requested_clo="CLO-1")
    assert report.rate("metadata_in_range") == 1.0
    assert report.rate("clo_alignment") == 1.0
    assert report.rate("labelled_and_sourced") == 1.0
    assert pq.difficulty_is_derived is True


# --- the check must never pass on absent data ------------------------------


def test_absent_marks_and_difficulty_is_not_run_not_passed():
    """The defect this whole checkpoint existed to find. `PracticeQuestion` had
    neither field, so the metric would have read 1.00 on every question."""
    report = score([gen(marks=None, difficulty=None)], BANK)
    assert report.rate("metadata_in_range") is None, "absent data must not score"
    assert report.as_dict()["metadata_in_range"] is None


def test_a_bank_with_no_usable_band_is_not_run_either():
    """Same defect from the other side: with fewer than two marked questions the
    bank cannot describe a range, so there is nothing to compare against. This
    previously recorded a PASS."""
    one = [{"question_id": "P1", "text": "x", "marks": 100, "difficulty": 0.5}]
    assert score([gen(marks=80, difficulty=0.4)], one).rate("metadata_in_range") is None


def test_one_comparable_field_is_enough_to_run_the_check():
    """Partial data still measures what it can. Marks present and in range,
    difficulty absent — the check runs on marks alone rather than skipping."""
    report = score([gen(marks=8, difficulty=None)], BANK)
    assert report.rate("metadata_in_range") == 1.0


def test_out_of_range_still_fails_when_data_is_present():
    """Existing valid behaviour, unchanged."""
    assert rate(score([gen(marks=500, difficulty=0.5)], BANK), "metadata_in_range") == 0.0
    assert rate(score([gen(marks=8, difficulty=99.0)], BANK), "metadata_in_range") == 0.0


# --- regression: multi-CLO sets must be scored per question ----------------


def test_a_multi_clo_batch_loses_clo_alignment():
    """Pins WHY `score_per_question` exists. `score()` takes one `requested_clo`;
    a set spanning several outcomes has no single right answer to pass, so a
    batch call reports the metric as "not run" — absent, not wrong, which is the
    hardest kind of gap to notice in a report."""
    from feature_b_rubric import score

    batch = [gen(clo_id="CLO-1"), gen(clo_id="CLO-2")]
    assert score(batch, BANK).rate("clo_alignment") is None


def test_score_per_question_keeps_clo_alignment_measurable():
    """The fix, pinned. Each question is scored against the CLO it was asked
    for, so the metric is real — and a wrong tag still fails."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from run_generation_eval import score_per_question

    on_2 = gen(clo_id="CLO-2",
               text="Describe how the scheduling quantum changes turnaround time.")
    right = [("CLO-1", "medium", gen()), ("CLO-2", "medium", on_2)]
    assert score_per_question(right, BANK).rate("clo_alignment") == 1.0

    # second asks for CLO-2 but the text is about CLO-1's topic
    half = [("CLO-1", "medium", gen()), ("CLO-2", "medium", gen())]
    assert score_per_question(half, BANK).rate("clo_alignment") == 0.5


def test_score_per_question_carries_the_pairing_in_the_data():
    """`[(requested_clo, question)]` rather than two parallel lists, so the
    pairing cannot drift out of order — a mis-zip would silently score every
    question against the wrong outcome."""
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from run_generation_eval import score_per_question

    params = list(inspect.signature(score_per_question).parameters)
    assert params == ["generated", "bank"]
    # (requested_clo, requested_band, question) — both requests travel WITH the
    # question so neither can drift out of order against a parallel list.
    assert score_per_question([], BANK).rate("clo_alignment") is None


# --- the quality metrics must fail on bad TEXT with perfect metadata --------
#
# This block is the point of the Phase 1B rubric redesign. A 20-question run
# scored 1.000 on three of four metrics while measuring nothing: the generator
# injects `clo_id`, `marks` and `difficulty` from the source record it selected,
# so comparing those against that source could never fail. Every test here gives
# the question FLAWLESS provenance metadata and a bad body, and requires the
# quality metric to fail anyway.


def _perfect_metadata(text: str, **kw) -> dict:
    """Provenance exactly as the pipeline injects it. Only the words are wrong."""
    return {"text": text, "clo_id": "CLO-1", "ai_generated": True,
            "derived_from": ["P1", "block-v1:lesson"], "marks": 10,
            "difficulty": 0.6, **kw}


def test_clo_alignment_fails_on_an_off_topic_question_with_perfect_metadata():
    """The exact tautology that was found. `clo_id` says CLO-1, the request says
    CLO-1, and the check still fails because the words are about CLO-2."""
    bad = _perfect_metadata("Describe the round robin scheduling quantum in detail.")
    report = score([bad], BANK, requested_clo="CLO-1")

    assert report.rate("clo_alignment") == 0.0
    assert report.rate("labelled_and_sourced") == 1.0, "provenance is untouched"
    assert report.rate("metadata_in_range") == 1.0, "metadata is untouched"


def test_band_plausibility_fails_when_the_verb_is_the_wrong_level():
    """Asked for a hard question, the model wrote a recall prompt. §7.6 derives
    difficulty from the command verb, so this reads the same signal the bank's
    own difficulty was built from."""
    bad = _perfect_metadata("State the definition of a deadlock.")
    report = score([bad], BANK, requested_clo="CLO-1", requested_band="hard")

    assert report.rate("band_plausibility") == 0.0
    assert report.rate("metadata_in_range") == 1.0, "metadata is untouched"


def test_band_plausibility_passes_when_the_verb_matches():
    good = _perfect_metadata(
        "Critically evaluate why a deadlock cannot resolve once processes hold and wait."
    )
    assert score([good], BANK, requested_clo="CLO-1",
                 requested_band="hard").rate("band_plausibility") == 1.0


def test_an_unrecognised_command_verb_is_not_run_not_passed():
    """A question whose verb is in no Bloom set cannot be judged. Same rule every
    other check follows: say so rather than pass."""
    odd = _perfect_metadata("Deadlock: two processes, one resource each. Thoughts?")
    assert score([odd], BANK, requested_clo="CLO-1",
                 requested_band="hard").rate("band_plausibility") is None


def test_a_reprint_still_fails_with_perfect_metadata():
    """`not_a_duplicate` was already text-based and is unchanged — pinned here so
    the redesign did not disturb it."""
    bad = _perfect_metadata(BANK[0]["text"])
    report = score([bad], BANK, requested_clo="CLO-1")

    assert report.rate("not_a_duplicate") == 0.0
    assert report.rate("labelled_and_sourced") == 1.0


def test_all_three_quality_metrics_can_fail_at_once():
    """The end-to-end demonstration: one intentionally bad question, flawless
    provenance, and every quality metric fails while every safety metric holds."""
    bad = _perfect_metadata(BANK[2]["text"])  # a reprint, off-topic, easy verb
    report = score([bad], BANK, requested_clo="CLO-1", requested_band="hard")

    assert report.rate("clo_alignment") == 0.0
    assert report.rate("band_plausibility") == 0.0
    assert report.rate("not_a_duplicate") == 0.0
    assert report.rate("labelled_and_sourced") == 1.0
    assert report.rate("metadata_in_range") == 1.0


def test_quality_and_safety_are_reported_apart():
    """Averaging an invariant that cannot move into a quality score would flatter
    the quality number."""
    from feature_b_rubric import QUALITY_CHECKS, SAFETY_CHECKS

    out = score([gen()], BANK, requested_clo="CLO-1", requested_band="medium").as_dict()
    assert set(out["quality"]) == set(QUALITY_CHECKS)
    assert set(out["safety"]) == set(SAFETY_CHECKS)
    assert not set(QUALITY_CHECKS) & set(SAFETY_CHECKS)
    # every rate carries its own n, because checks can individually not-run
    assert all("n" in v for v in {**out["quality"], **out["safety"]}.values())


def test_alignment_cannot_run_with_a_single_outcome():
    """With one vocabulary the requested outcome is trivially nearest — the same
    tautology in miniature. Report not-run instead."""
    one_clo = [q for q in BANK if q["clo_id"] == "CLO-1"]
    assert score([gen()], one_clo, requested_clo="CLO-1").rate("clo_alignment") is None
