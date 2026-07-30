"""Cache keys — design §6.4.

Three tiers with distinct keys and invalidation rules. They fail differently, and
the difference is the reason they are not one cache: a stale **response** cache is
a correctness and security bug; a stale **embedding** cache is a cost miss.

The response key carries a fix from design v4 that is easy to lose again:

    Keying only on the query string meant a course-staff member's answer —
    retrieved from a wider candidate set — could be served to a student who
    asked the same question.

So the key hashes the caller's **effective permission scope** and the **applied
filters** alongside the query. Every test in `test_cache_keys.py` that looks
paranoid is defending that one sentence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


def _digest(payload: object) -> str:
    """Stable hash. `sort_keys` matters: dict ordering must not change the key."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def embedding_key(chunk_text: str, embedding_model_id: str) -> str:
    """Content-addressed, so it is never stale — only evicted (LRU).

    This tier pairs with verified platform behaviour: because a publish event
    fires for the *container* (§3.2), a section-level publish re-resolves every
    leaf in that section, most unchanged. Dedup on usage_key@version skips most;
    this catches the rest. Together they turn a section publish from an
    O(section) re-embed into O(changed leaves).
    """
    return f"emb:{embedding_model_id}:{_digest(chunk_text)}"


def response_key(
    *,
    tenant: str,
    offering_id: str,
    course_version: str,
    effective_scope: Mapping[str, object],
    applied_filters: Mapping[str, object],
    normalized_query: str,
    mode: str,
) -> str:
    """Key for a cached answer.

    `effective_scope` is the caller's *resolved* permission scope — roles and
    enrollment as the boundary derived them, not as the caller claimed them.
    Two callers with different scopes must never collide here even when every
    other component is identical.
    """
    return "resp:" + _digest(
        {
            "tenant": tenant,
            "offering_id": offering_id,
            "course_version": course_version,
            "scope": dict(effective_scope),
            "filters": dict(applied_filters),
            "query": normalized_query.strip().lower(),
            "mode": mode,
        }
    )


def permission_key(student_id: str, course_id: str) -> str:
    """Metadata tier — permissions. Deliberately the shortest-lived cache (§6.4),
    because a *revoked* enrollment must stop working quickly. Also invalidated
    directly by LMS enrollment events (§3.4 rule 4)."""
    return f"perm:{student_id}:{course_id}"


def clo_map_key(offering_id: str) -> str:
    """Metadata tier — CLO map. Invalidated on CLO edits."""
    return f"clo:{offering_id}"


def normalize_query(query: str) -> str:
    """Whitespace and case only.

    Deliberately conservative: aggressive normalisation raises the hit rate and
    starts serving answers to questions nobody asked.
    """
    return " ".join(query.split()).lower()


def scope_of(
    *, student_id: str, roles: Sequence[str], enrolled_offerings: Sequence[str]
) -> dict[str, object]:
    """Build the effective-scope component of a response key.

    Roles and offerings are sorted so that an ordering difference upstream cannot
    produce two keys for one scope — a cache miss is cheap, but the inverse bug
    (two scopes colliding on one key) is the staff-to-student leak this exists to
    prevent, and sorting is what keeps the mapping one-way.
    """
    return {
        "student": student_id,
        "roles": sorted(set(roles)),
        "offerings": sorted(set(enrolled_offerings)),
    }
