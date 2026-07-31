"""Knowledge layer — the store, its index, and the isolation filters.

Reachable from the reasoning layer only through the CourseIntelligence boundary
(§6.5). `.importlinter` enforces that: four things must happen on every data
access — resolve identity, check scope, filter before ranking, audit — and behind
one interface they are a chokepoint a new caller cannot forget.
"""

from __future__ import annotations

from functools import lru_cache

from .store import ChunkStore


@lru_cache(maxsize=1)
def get_store() -> ChunkStore:
    from ..config import settings

    return ChunkStore(settings.index_path)


__all__ = ["ChunkStore", "get_store"]
