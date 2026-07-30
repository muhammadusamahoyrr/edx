"""Platform-side state.

Two tables, and both exist to satisfy Principle 8 — *never fail silently*. A gap
in the index must be **detectable**, never a quiet absence that surfaces later as
"the tutor doesn't know that lesson."
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class CourseIndexState(models.Model):
    """Resumable bootstrap progress for one course.

    Not a `last_indexed_at` display field. The platform's own `reindex_studio`
    tracks completion state and resumes if interrupted, and it does so because a
    process that indexes hundreds of blocks in one go *will* be interrupted —
    a closed terminal, a restarted worker, an OOM kill. Restarting from zero each
    time is how a large course never finishes indexing at all.
    """

    course_id = models.CharField(max_length=255, unique=True, db_index=True)

    block_count = models.PositiveIntegerField(default=0)
    blocks_indexed = models.PositiveIntegerField(default=0)
    #: Walk position, so a resumed run continues rather than restarts.
    last_usage_key = models.CharField(max_length=255, blank=True, default="")

    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        app_label = "coursemate_platform"

    @classmethod
    def for_course(cls, course_id: str) -> "CourseIndexState":
        state, _ = cls.objects.get_or_create(course_id=course_id)
        return state

    def last_indexed_display(self) -> str:
        if not self.last_indexed_at:
            return "never"
        return timezone.localtime(self.last_indexed_at).strftime("%Y-%m-%d %H:%M")

    @property
    def is_complete(self) -> bool:
        return self.block_count > 0 and self.blocks_indexed >= self.block_count


class FailedIngestion(models.Model):
    """A block that failed all retries.

    The reconciliation sweep reports on these (§5.2, §5.4). This table is the
    difference between a detectable gap and a silent one — without it, a block
    that failed to embed simply never appears, and the tutor reports "not covered
    in this course," which reads as correct behaviour.
    """

    course_id = models.CharField(max_length=255, db_index=True)
    usage_key = models.CharField(max_length=255, db_index=True)
    version = models.CharField(max_length=255, blank=True, default="")

    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=1)
    first_failed_at = models.DateTimeField(auto_now_add=True)
    last_failed_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "coursemate_platform"
        unique_together = ("course_id", "usage_key")

    @classmethod
    def record(cls, course_id: str, usage_key: str, version: str, error: str) -> None:
        row, created = cls.objects.get_or_create(
            course_id=course_id,
            usage_key=usage_key,
            defaults={"version": version, "error": error},
        )
        if not created:
            row.attempts += 1
            row.error = error
            row.version = version
            row.resolved_at = None
            row.save(update_fields=["attempts", "error", "version", "resolved_at", "last_failed_at"])
