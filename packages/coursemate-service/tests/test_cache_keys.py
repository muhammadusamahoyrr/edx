"""Cache isolation rules — for a cache that does not exist yet.

**These tests currently pass vacuously.** No response cache is wired into the
request path, so nothing they protect is running. They are written now because
the rules are the expensive part, not the code: two of them encode bugs already
found once (a staff-scope answer served to a student, and personal uploads
surviving in a cache after every filter was correct).

They stop being vacuous the moment a cache is added, and they will fail loudly if
it is added wrongly. Do not read a green run here as evidence that caching is
safe today — read it as the specification the cache must satisfy.


Design §10.2 names caching as "how isolation quietly fails *after* all the filters
are written correctly" — a bug class that passes code review, produces no error,
and is discovered by a customer. These are cheap and get written the day the cache
lands, per the build plan's testing triage.
"""

from __future__ import annotations

import pytest
from coursemate_contracts.metadata import ChunkMetadata, ContentType
from coursemate_service.knowledge.cache import keys
from coursemate_service.knowledge.cache.policy import (
    PersonalDataNotCacheable,
    assert_cacheable,
    touched_personal_namespace,
)

COMMON = dict(
    tenant="acme",
    offering_id="CS101-2026-FALL",
    course_version="v9",
    applied_filters={"content_type": "lesson"},
    normalized_query="what is a deadlock",
    mode="direct",
)


def test_staff_and_student_never_share_a_response_key():
    """The v4 fix, defended.

    Keying only on the query string meant a course-staff member's answer —
    retrieved from a wider candidate set — could be served to a student who asked
    the same question. Same query, same course, same everything except scope.
    """
    student = keys.response_key(
        effective_scope=keys.scope_of(
            student_id="u1", roles=["student"], enrolled_offerings=["CS101-2026-FALL"]
        ),
        **COMMON,
    )
    staff = keys.response_key(
        effective_scope=keys.scope_of(
            student_id="u2",
            roles=["student", "staff"],
            enrolled_offerings=["CS101-2026-FALL"],
        ),
        **COMMON,
    )
    assert student != staff


def test_two_students_in_the_same_course_do_not_share_a_key():
    a = keys.response_key(
        effective_scope=keys.scope_of(
            student_id="u1", roles=["student"], enrolled_offerings=["CS101-2026-FALL"]
        ),
        **COMMON,
    )
    b = keys.response_key(
        effective_scope=keys.scope_of(
            student_id="u2", roles=["student"], enrolled_offerings=["CS101-2026-FALL"]
        ),
        **COMMON,
    )
    assert a != b


def test_role_ordering_does_not_produce_two_keys_for_one_scope():
    """A cache miss is cheap; two scopes colliding on one key is the leak. Sorting
    is what keeps that mapping one-way."""
    one = keys.scope_of(student_id="u1", roles=["staff", "student"], enrolled_offerings=["a", "b"])
    two = keys.scope_of(student_id="u1", roles=["student", "staff"], enrolled_offerings=["b", "a"])
    assert one == two


def test_course_version_bump_invalidates():
    before = keys.response_key(
        effective_scope=keys.scope_of(student_id="u1", roles=["student"], enrolled_offerings=[]),
        **COMMON,
    )
    after_args = dict(COMMON, course_version="v10")
    after = keys.response_key(
        effective_scope=keys.scope_of(student_id="u1", roles=["student"], enrolled_offerings=[]),
        **after_args,
    )
    assert before != after


def test_different_filters_are_different_answers():
    """Filters are applied before ranking, so two filter sets are two candidate
    sets and cannot share a cached answer."""
    narrow = dict(COMMON, applied_filters={"content_type": "lesson", "week": 4})
    a = keys.response_key(
        effective_scope=keys.scope_of(student_id="u1", roles=[], enrolled_offerings=[]),
        **COMMON,
    )
    b = keys.response_key(
        effective_scope=keys.scope_of(student_id="u1", roles=[], enrolled_offerings=[]),
        **narrow,
    )
    assert a != b


def test_embedding_key_is_content_addressed():
    """Never stale, only evicted — so the same text under the same model is always
    the same key, and a different model is never a false hit."""
    assert keys.embedding_key("hello", "m1") == keys.embedding_key("hello", "m1")
    assert keys.embedding_key("hello", "m1") != keys.embedding_key("hello", "m2")
    assert keys.embedding_key("hello", "m1") != keys.embedding_key("hello!", "m1")


def _chunk(student: str | None) -> ChunkMetadata:
    return ChunkMetadata(
        tenant="acme",
        course_id="CS101",
        offering_id="CS101-2026-FALL",
        usage_key="block-v1:...",
        block_id="abc",
        block_type="html",
        version="1",
        content_type=ContentType.LESSON if student is None else ContentType.STUDENT_NOTE,
        student=student,
    )


def test_personal_results_are_never_cacheable():
    """§6.4: not stored, not served. A security control, not an optimisation."""
    retrieved = [_chunk(None), _chunk("student-42")]
    assert touched_personal_namespace(retrieved)
    with pytest.raises(PersonalDataNotCacheable):
        assert_cacheable(retrieved)


def test_course_only_results_are_cacheable():
    assert_cacheable([_chunk(None), _chunk(None)])


def test_the_check_is_on_what_retrieval_touched_not_what_was_cited():
    """A chunk that influenced an answer has already leaked into it, whether or
    not it ended up in a citation."""
    with pytest.raises(PersonalDataNotCacheable):
        assert_cacheable([_chunk("student-7")])
