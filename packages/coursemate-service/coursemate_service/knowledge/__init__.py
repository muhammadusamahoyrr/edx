"""Knowledge layer — the store, its index, and the isolation filters.

Reachable from the reasoning layer only through the CourseIntelligence boundary
(§6.5). `.importlinter` enforces that: four things must happen on every data
access — resolve identity, check scope, filter before ranking, audit — and behind
one interface they are a chokepoint a new caller cannot forget.
"""

from __future__ import annotations

from functools import lru_cache

from .examprep_store import ExamPrepStore
from .store import ChunkStore


@lru_cache(maxsize=1)
def get_store() -> ChunkStore:
    from ..config import settings

    return ChunkStore(settings.index_path)


@lru_cache(maxsize=1)
def get_examprep_store() -> ExamPrepStore:
    """Past-paper questions. Its own file, not a second set of tables in the index.

    Separate because the two have different lifecycles: a course reindex rewrites
    the chunk index wholesale and must not be able to take a term's worth of
    extracted past papers with it. They are also restored from different sources —
    the modulestore rebuilds one, only the original PDFs rebuild the other.
    """
    from ..config import settings

    return ExamPrepStore(settings.examprep_path)


__all__ = ["ChunkStore", "ExamPrepStore", "get_examprep_store", "get_store"]
