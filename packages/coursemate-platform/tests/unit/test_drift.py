"""The sweep's decision, which is the part that deletes content.

`reconcile_course` needs a modulestore, a broker and a running service. What it
*concludes* needs none of them, so these run in milliseconds and cover the cases
that actually hurt.
"""

from __future__ import annotations

from coursemate_platform.drift import compute_drift


def test_orphans_are_indexed_minus_live():
    """The whole reason the sweep exists: no unpublish event, so an unpublished
    block is only ever detectable as a difference between these two sets."""
    d = compute_drift(live_keys={"a", "b"}, indexed_keys={"a", "b", "gone"})
    assert d.orphans == ["gone"]
    assert d.missing == []


def test_missing_are_live_minus_indexed():
    d = compute_drift(live_keys={"a", "b"}, indexed_keys={"a"})
    assert d.missing == ["b"]
    assert d.orphans == []


def test_a_matching_course_is_clean():
    assert compute_drift(live_keys={"a"}, indexed_keys={"a"}).is_clean


def test_empty_course_read_never_wipes_the_index():
    """The failure this cap exists for.

    `iter_course_leaves` yields nothing when the course is missing on the
    published branch — which also happens on a transient modulestore failure.
    Naive subtraction marks every block an orphan and the sweep deletes the
    course's whole index while logging a successful run.
    """
    d = compute_drift(live_keys=set(), indexed_keys={"a", "b", "c"})
    assert d.orphans == [], "must not propose deleting the entire index"
    assert d.refused
    assert "0 published blocks" in d.refused


def test_mass_unpublish_is_refused_not_guessed():
    live = {f"k{i}" for i in range(2)}
    indexed = {f"k{i}" for i in range(10)}
    d = compute_drift(live, indexed)
    assert d.orphans == []
    assert "80%" in d.refused


def test_a_normal_unpublish_is_under_the_cap_and_proceeds():
    """The common case must not need an operator. One unit out of ten goes."""
    live = {f"k{i}" for i in range(9)}
    indexed = {f"k{i}" for i in range(10)}
    d = compute_drift(live, indexed)
    assert d.orphans == ["k9"]
    assert not d.refused


def test_force_lifts_the_cap():
    """The legitimate large unpublish, after a human looked at the course."""
    d = compute_drift(live_keys=set(), indexed_keys={"a", "b"}, force=True)
    assert d.orphans == ["a", "b"]
    assert not d.refused


def test_an_unindexed_course_is_not_drift():
    """Nothing is served yet, so nothing can be orphaned. Reporting the whole
    course as 'missing' is correct; treating it as drift to repair would let a
    nightly job silently enable the tutor for a course nobody opted in."""
    d = compute_drift(live_keys={"a", "b"}, indexed_keys=set())
    assert d.orphans == []
    assert d.missing == ["a", "b"]
    assert not d.refused


def test_refusal_still_reports_missing_blocks():
    """A refusal to delete must not also suppress the repair half of the sweep —
    the two decisions are independent."""
    d = compute_drift(live_keys={"new"}, indexed_keys={"a", "b", "c"})
    assert d.refused
    assert d.missing == ["new"]


def test_output_is_ordered():
    """Sorted so a report diffed across two nights is readable."""
    d = compute_drift(live_keys=set(), indexed_keys={"c", "a", "b"}, force=True)
    assert d.orphans == ["a", "b", "c"]
