"""Cache policy — design §6.4 and §10.2.

**STATUS: wired as of Phase C2.** `coursemate_service.response_cache.write` calls
`assert_cacheable` before storing any answer, so this is now an active control
rather than a documented intention. It was specified and tested for six phases
before anything called it, which is how the rule survived to be inherited.


This module exists as its own file for one reason: the rule *"personal-namespace
results are never cached"* is stated in two design sections and would otherwise be
implemented in three cache tiers. A rule implemented three times is a rule that
holds in two of them.

§10.2 calls this a security control, not an optimisation, and gives the reason:
caching is how isolation quietly fails **after** the filters are all written
correctly. Every filter can be right and the cache still hands one student's
private past paper to another.
"""

from __future__ import annotations

from collections.abc import Iterable

from coursemate_contracts.metadata import ChunkMetadata


class PersonalDataNotCacheable(RuntimeError):
    """Raised on an attempt to cache a response that touched a personal namespace.

    Loud on purpose. A silent skip would make the security control invisible, and
    an invisible control is one nobody notices has stopped working.
    """


def touched_personal_namespace(retrieved: Iterable[ChunkMetadata]) -> bool:
    return any(chunk.is_personal for chunk in retrieved)


def assert_cacheable(retrieved: Iterable[ChunkMetadata]) -> None:
    """Gate every response-cache write through this.

    Not stored, not served — §6.4. The check is on what retrieval *touched*, not
    on what the answer quotes, because a chunk that influenced an answer has
    already leaked into it whether or not it was cited.
    """
    materialised = list(retrieved)
    if touched_personal_namespace(materialised):
        raise PersonalDataNotCacheable(
            "Retrieval touched a per-student namespace; this response must not be "
            "cached (design §6.4, §10.2)."
        )
