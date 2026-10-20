import uuid

from django.db import models


class ResearchRun(models.Model):
    """Persisted record of one research-stream run.

    The SSE event stream is the execution trace; today it evaporates
    when the browser disconnects. This row stores the ordered event
    list plus the terminal result so runs are queryable, replayable,
    and usable as eval substrate. Written by the stream worker in
    worldkg_nca.views.research — the agent never writes directly.
    """

    STATUS_CHOICES = [
        ("running", "Running"),
        ("done", "Done"),
        ("error", "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trace_id = models.CharField(max_length=64, db_index=True)
    prompt = models.TextField()
    country_code = models.CharField(max_length=10, db_index=True)
    snapshot_date = models.CharField(max_length=20)
    subdivision_qid = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default="running", db_index=True
    )
    # Ordered SSE events as emitted ({event, ...} dicts, JSONB).
    events = models.JSONField(default=list)
    # The terminal `done` payload (questions, coverage, confidence,
    # tool_calls, summary) or the error message.
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    recipe = models.CharField(max_length=60, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["country_code", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.country_code} {self.prompt[:40]} ({self.status})"
