"""Retrieval and isolation.

The isolation tests here are the ones that must never be skipped: §6.3 requires
unauthorized content to never be a *candidate*, not merely never returned. A
post-filter would pass a naive test and still leak through result counts.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_service.knowledge.store import ChunkStore


def _rows(offering: str, version: str, texts: list[tuple[str, str]], tenant="default"):
    return [
        {
            "tenant": tenant, "course_id": offering, "offering_id": offering,
            "usage_key": f"block-v1:{offering}+type@html+block@{i}", "block_id": f"b{i}",
            "block_type": "html", "content_type": "lesson", "display_name": name,
            "version": version, "ordinal": 0, "text": text,
        }
        for i, (name, text) in enumerate(texts)
    ]


@pytest.fixture
def store(tmp_path) -> ChunkStore:
    return ChunkStore(tmp_path / "idx.db")


def test_write_verify_swap_makes_content_searchable(store):
    rows = _rows("CS101", "v1", [("Deadlocks", "A deadlock occurs when processes wait forever")])
    store.write_chunks(rows)
    assert store.verify("CS101", "v1", 1)
    # Before the swap nothing is retrievable — inactive chunks are not candidates.
    assert store.search("deadlock", tenant="default", offering_id="CS101") == []
    store.swap("CS101", "v1")
    hits = store.search("deadlock", tenant="default", offering_id="CS101")
    assert len(hits) == 1
    assert hits[0].display_name == "Deadlocks"


def test_failed_verify_leaves_previous_index_intact(store):
    store.write_chunks(_rows("CS101", "v1", [("L1", "original published content")]))
    store.swap("CS101", "v1")
    # A partial second ingest that fails verification must NOT be swapped in.
    store.write_chunks(_rows("CS101", "v2", [("L1", "half written")]))
    assert store.verify("CS101", "v2", 99) is False
    hits = store.search("original", tenant="default", offering_id="CS101")
    assert hits and "original" in hits[0].text, "previous good state was lost"


def test_swap_removes_superseded_versions(store):
    store.write_chunks(_rows("CS101", "v1", [("L1", "alpha content here")]))
    store.swap("CS101", "v1")
    store.write_chunks(_rows("CS101", "v2", [("L1", "beta content here")]))
    store.swap("CS101", "v2")
    hits = store.search("content", tenant="default", offering_id="CS101")
    assert len(hits) == 1, "stale and current content coexisted"
    assert "beta" in hits[0].text


def test_offerings_are_isolated(store):
    store.write_chunks(_rows("CS101", "v1", [("L", "unique_marker_alpha appears here")]))
    store.swap("CS101", "v1")
    store.write_chunks(_rows("BIO200", "v1", [("L", "unique_marker_alpha appears here too")]))
    store.swap("BIO200", "v1")
    hits = store.search("unique_marker_alpha", tenant="default", offering_id="CS101")
    assert len(hits) == 1
    assert all("CS101" in h.usage_key for h in hits)


def test_tenants_are_isolated(store):
    store.write_chunks(_rows("CS101", "v1", [("L", "tenant_scoped_text")], tenant="acme"))
    store.swap("CS101", "v1")
    assert store.search("tenant_scoped_text", tenant="other", offering_id="CS101") == []
    assert store.search("tenant_scoped_text", tenant="acme", offering_id="CS101")


def test_scores_are_normalised_and_ordered(store):
    store.write_chunks(_rows("CS101", "v1", [
        ("Exact", "deadlock deadlock deadlock concurrency"),
        ("Weak", "an unrelated lesson about typography"),
    ]))
    store.swap("CS101", "v1")
    hits = store.search("deadlock", tenant="default", offering_id="CS101")
    assert hits[0].display_name == "Exact"
    assert 0.0 < hits[0].score <= 1.0


def test_fts_syntax_in_a_question_cannot_break_the_query(store):
    """A student's question is untrusted input. FTS5 operators must not be
    interpreted as query syntax (§10.6)."""
    store.write_chunks(_rows("CS101", "v1", [("L", "grading policy explained")]))
    store.swap("CS101", "v1")
    for hostile in ['grading OR "', 'policy NEAR/', '*', '"unclosed', 'a AND (b']:
        store.search(hostile, tenant="default", offering_id="CS101")  # must not raise


def _claims(offering="CS101"):
    now = int(time.time())
    return StudentClaims(sub="u1", course_id=offering, offering_id=offering,
                         roles=["student"], aud=AUDIENCE_STUDENT, exp=now + 300, iat=now,
                         usage_key="u", block_id="b")


def test_boundary_denies_cross_offering_access(store, monkeypatch):
    """The token scopes the caller. Asking for another offering is refused at the
    boundary, not filtered afterwards."""
    from coursemate_service.boundary.impl import AuthorizationError, CourseIntelligenceImpl
    import coursemate_service.knowledge as knowledge

    monkeypatch.setattr(knowledge, "get_store", lambda: store)
    import coursemate_service.boundary.impl as impl
    monkeypatch.setattr(impl, "get_store", lambda: store)

    with pytest.raises(AuthorizationError):
        CourseIntelligenceImpl().retrieve_course_context("q", "BIO200", _claims("CS101"))


def test_multi_batch_run_keeps_every_batch(store):
    """Regression: the swap boundary is the RUN, not the batch.

    Each batch used to swap itself in and deactivate its predecessors, so a
    226-block course ended up serving only its last 26 blocks — while every
    batch reported success. Nothing failed; the content simply vanished.
    """
    run = "run-1"
    store.write_chunks(_rows("CS101", run, [("A", "alpha lesson content")]))
    store.write_chunks(_rows("CS101", run, [("B", "bravo lesson content")]))
    store.write_chunks(_rows("CS101", run, [("C", "charlie lesson content")]))
    assert store.verify_run("CS101", run)
    store.swap("CS101", run)

    hits = store.search("lesson", tenant="default", offering_id="CS101", limit=10)
    assert len(hits) == 3, f"expected all 3 batches active, got {len(hits)}"
    assert store.stats("CS101")["chunk_count"] == 3


def test_empty_run_is_refused_rather_than_swapped(store):
    """An ingest that wrote nothing must not replace a working index with an
    empty one — that would turn a live tutor into 'still being prepared'."""
    store.write_chunks(_rows("CS101", "good", [("A", "existing content")]))
    store.swap("CS101", "good")
    assert store.verify_run("CS101", "empty-run") is False
    assert store.search("existing", tenant="default", offering_id="CS101")


def test_scores_are_absolute_not_relative_to_the_result_set(store):
    """Regression: the confidence gate must be able to fire.

    Scores were normalised against the best row of the SAME query, so the top hit
    was 1.0 for every query however weak, and the threshold never fired. The
    tutor answered 'explain quantum chromodynamics' from an unrelated lesson
    while groundedness read 1.0 — the model faithfully grounded its answer in an
    irrelevant chunk.
    """
    store.write_chunks(_rows("CS101", "v1", [
        ("Transcripts", "video transcripts help learners with hearing impairments"),
    ]))
    store.swap("CS101", "v1")

    on_topic = store.search("video transcripts", tenant="default", offering_id="CS101")
    assert on_topic, "expected the on-topic query to retrieve something"
    assert on_topic[0].score >= 0.5, f"on-topic score too low: {on_topic[0].score}"

    # A query sharing ONE incidental word must not score like a real match.
    off_topic = store.search(
        "quantum chromodynamics colour confinement video", tenant="default", offering_id="CS101"
    )
    if off_topic:
        assert off_topic[0].score < 0.35, (
            f"off-topic query scored {off_topic[0].score} — the gate cannot fire"
        )


def test_score_does_not_depend_on_how_many_rows_come_back(store):
    """The bug's mechanism: a single weak row normalised to 1.0 because it was
    the best row present. Score must be a property of the match, not the set."""
    store.write_chunks(_rows("CS101", "v1", [
        ("Only", "an isolated lesson about typography and layout"),
    ]))
    store.swap("CS101", "v1")
    hits = store.search("typography deadlock concurrency scheduling", tenant="default", offering_id="CS101")
    if hits:
        assert hits[0].score < 1.0, "sole result was scored as a perfect match"


def test_reranker_promotes_a_title_match_over_a_body_mention(store):
    """The signal BM25 cannot see: an instructor named the block.

    A lesson called "Transcripts" is strong evidence for a question about
    transcripts, and display_name is metadata we never index into FTS.
    """
    from coursemate_service.knowledge.rerank import LexicalReranker

    store.write_chunks(_rows("CS101", "v1", [
        ("Video Settings", "many lessons mention transcripts in passing among other topics here"),
        ("Transcripts", "transcripts help learners follow along with recorded material"),
    ]))
    store.swap("CS101", "v1")
    candidates = store.search("transcripts", tenant="default", offering_id="CS101", limit=20)
    ranked = LexicalReranker().rerank("transcripts", candidates, top_k=2)
    assert ranked[0].display_name == "Transcripts"


def test_reranker_updates_the_score_it_hands_to_the_gate(store):
    """A reranker that reordered without rescoring would leave the confidence
    gate reading a number that describes a different ranking."""
    from coursemate_service.knowledge.rerank import LexicalReranker

    store.write_chunks(_rows("CS101", "v1", [("Cohorts", "cohorts group learners for discussion")]))
    store.swap("CS101", "v1")
    candidates = store.search("cohorts", tenant="default", offering_id="CS101", limit=20)
    ranked = LexicalReranker().rerank("cohorts", candidates, top_k=1)
    assert 0.0 <= ranked[0].score <= 1.0


def test_null_reranker_is_a_pure_passthrough(store):
    """The control arm must not perturb the ordering it is measuring against."""
    from coursemate_service.knowledge.rerank import NullReranker

    store.write_chunks(_rows("CS101", "v1", [
        ("A", "alpha content about cohorts"), ("B", "beta content about cohorts"),
    ]))
    store.swap("CS101", "v1")
    candidates = store.search("cohorts", tenant="default", offering_id="CS101", limit=20)
    assert NullReranker().rerank("cohorts", candidates, 2) == candidates[:2]


# --- block-level access ----------------------------------------------------
#
# These belong with the isolation tests above, not in a file of their own: they
# defend the same rule one level deeper. Course isolation stops a student
# reading another course; these stop a student reading content their own course
# restricted to a cohort or a paid track.


def _restricted(offering, version, name, text, tokens, i=99):
    return [{
        "tenant": "default", "course_id": offering, "offering_id": offering,
        "usage_key": f"block-v1:{offering}+type@html+block@{i}", "block_id": f"b{i}",
        "block_type": "html", "content_type": "lesson", "display_name": name,
        "version": version, "ordinal": 0, "text": text, "group_tokens": tokens,
    }]


def test_unrestricted_content_reaches_a_caller_with_no_groups(store):
    """The common case. Most blocks carry no restriction and must not need one."""
    store.write_chunks(_rows("CS101", "v1", [("Intro", "cohorts explained simply")]))
    store.swap("CS101", "v1")
    hits = store.search("cohorts", tenant="default", offering_id="CS101")
    assert len(hits) == 1


def test_restricted_content_is_hidden_from_a_caller_without_the_group(store):
    store.write_chunks(
        _restricted("CS101", "v1", "Graded", "cohorts graded exam answer", ("50:2",))
    )
    store.swap("CS101", "v1")
    # An audit student: enrolled, but not in the verified group.
    assert store.search("cohorts", tenant="default", offering_id="CS101") == []


def test_restricted_content_reaches_a_caller_holding_the_group(store):
    """The half that a blunt index-time filter would break: a student who paid
    must still receive the content they paid for."""
    store.write_chunks(
        _restricted("CS101", "v1", "Graded", "cohorts graded exam answer", ("50:2",))
    )
    store.swap("CS101", "v1")
    hits = store.search(
        "cohorts", tenant="default", offering_id="CS101",
        group_tokens=frozenset({"50:2"}),
    )
    assert len(hits) == 1
    assert hits[0].display_name == "Graded"


def test_holding_an_unrelated_group_does_not_unlock_content(store):
    store.write_chunks(
        _restricted("CS101", "v1", "Graded", "cohorts graded exam answer", ("50:2",))
    )
    store.swap("CS101", "v1")
    assert store.search(
        "cohorts", tenant="default", offering_id="CS101",
        group_tokens=frozenset({"50:1", "77:9"}),
    ) == []


def test_restriction_is_dropped_when_its_chunk_is(store):
    """A stale chunk_groups row would re-restrict whatever id SQLite reuses next,
    which is invisible until an unrelated chunk quietly stops being returned."""
    store.write_chunks(
        _restricted("CS101", "v1", "Graded", "cohorts graded exam answer", ("50:2",))
    )
    store.swap("CS101", "v1")
    store.delete_usage_keys("CS101", ["block-v1:CS101+type@html+block@99"])
    left = store._conn.execute("SELECT COUNT(*) FROM chunk_groups").fetchone()[0]
    assert left == 0
