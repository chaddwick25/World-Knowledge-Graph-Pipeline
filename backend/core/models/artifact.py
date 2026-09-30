from django.db import models
import uuid
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django_prometheus.models import ExportModelOperationsMixin



class CountryArtifact(models.Model):
    """
    Track all artifacts generated for a country.
    Enables artifact selection for downstream tasks.
    """
    
    class ArtifactType(models.TextChoices):
        REGION_EXTRACT = 'REGION_EXTRACT', 'Region Extract'
        YEARLY_EXTRACT = 'YEARLY_EXTRACT', 'Yearly Extract'
        MONTHLY_EXTRACT = 'MONTHLY_EXTRACT', 'Monthly Extract'
        ASSET_BUNDLE = 'ASSET_BUNDLE', 'Asset Bundle'
        FASTTEXT_MODEL = 'FASTTEXT_MODEL', 'FastText Model'
        EMBEDDINGS = 'EMBEDDINGS', 'Embeddings'
    
    class ArtifactStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        DELETED = 'DELETED', 'Deleted'
        ARCHIVED = 'ARCHIVED', 'Archived'
        BACKED_UP = 'BACKED_UP', 'Backed Up'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_code = models.CharField(max_length=3, db_index=True)
    country_name = models.CharField(max_length=100, db_index=True)
    
    artifact_type = models.CharField(max_length=20, choices=ArtifactType.choices)
    artifact_path = models.TextField(help_text="File path or database reference")
    artifact_status = models.CharField(
        max_length=20, 
        choices=ArtifactStatus.choices, 
        default=ArtifactStatus.ACTIVE
    )
    
    # Temporal metadata
    temporal_start = models.DateField(null=True, blank=True)
    temporal_end = models.DateField(null=True, blank=True)
    
    # File metadata
    file_size_mb = models.FloatField(null=True, blank=True)
    file_hash = models.CharField(max_length=64, null=True, blank=True)
    
    # TODO: remove this legacy code
    google_drive_path = models.TextField(null=True, blank=True)
    backup_confirmed = models.BooleanField(default=False)
    
    # Lifecycle metadata
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deletion_reason = models.TextField(null=True, blank=True)
    
    # Relationships
    parent_artifact = models.ForeignKey(
        'self', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='derived_artifacts'
    )
    processing_session = models.ForeignKey(
        'core.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    # Metadata
    metadata = models.JSONField(default=dict, help_text="Additional artifact-specific metadata")
    
    class Meta:
        db_table = 'country_artifacts'
        indexes = [
            models.Index(fields=['country_code', 'artifact_type', 'artifact_status']),
            models.Index(fields=['country_code', 'temporal_start', 'temporal_end']),
        ]
    
    def __str__(self):
        return f"{self.country_name} - {self.get_artifact_type_display()} ({self.get_artifact_status_display()})"


class EmbeddingArtifact(models.Model):
    """
    Track embeddings stored in pgvector database.
    Links to CountryArtifact for complete lineage.
    """
    
    class EmbeddingType(models.TextChoices):
        GV_TAGS = 'GV_TAGS', 'GV-Tags'
        GV_NLE = 'GV_NLE', 'GV-NLE'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_artifact = models.ForeignKey(
        CountryArtifact,
        on_delete=models.CASCADE,
        related_name='embeddings'
    )
    # Embedding metadata
    embedding_type = models.CharField(max_length=20, choices=EmbeddingType.choices)
    embedding_dimension = models.IntegerField(default=300)
    total_entities = models.IntegerField()
    # Database reference (pgvector)
    table_name = models.CharField(max_length=100)
    index_type = models.CharField(max_length=20)  # ivfflat, hnsw
    # Training metadata
    trained_at = models.DateTimeField(auto_now_add=True)
    training_config = models.JSONField(default=dict)
    # Performance metrics
    training_time_seconds = models.FloatField(null=True, blank=True)
    index_build_time_seconds = models.FloatField(null=True, blank=True)
    
    class Meta:
        db_table = 'embedding_artifacts'
        indexes = [
            models.Index(fields=['country_artifact', 'embedding_type']),
        ]
    
    def __str__(self):
        return f"{self.country_artifact.country_name} - {self.get_embedding_type_display()} ({self.total_entities} entities)"


class PartitionRegistry(models.Model):
    """Tracks completion of each (snapshot, country, subdivision) partition.

    Every subgraph-level task checks the registry via ``get_or_create`` before
    touching a partition. If ``status == 'complete'``, the task skips
    processing — this provides idempotency for Celery retries and re-runs.

    The ``(snapshot_id, country_code, subdivision)`` triple uniquely identifies
    a leaf partition in the sharded embeddings table. ``subdivision`` is NULL
    for leaf / small countries that have no subgraph fan-out.
    """

    class PartitionStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETE = 'complete', 'Complete'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    snapshot_id = models.TextField(
        help_text="Snapshot identifier (e.g., '2025_12_31', or PlanetSnapshot UUID)",
    )
    country_code = models.TextField(
        help_text="ISO 3166-1 alpha-2 country code (e.g., 'BZ', 'MZ')",
    )
    subdivision = models.TextField(
        null=True,
        blank=True,
        help_text="Subgraph slug (e.g., 'cayo_district'). NULL for leaf countries.",
    )

    status = models.TextField(
        choices=PartitionStatus.choices,
        default=PartitionStatus.PENDING,
        help_text="Processing status of this partition",
    )

    entity_count = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Number of entities (rows) written to this partition",
    )
    embedding_count = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Number of embedding vectors indexed in this partition",
    )

    # Temporal tracking
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When processing of this partition started",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When processing of this partition completed or failed",
    )

    # Error details
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text="Error message if status is 'failed'",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'partition_registry'
        unique_together = [('snapshot_id', 'country_code', 'subdivision')]
        indexes = [
            models.Index(fields=['snapshot_id', 'country_code']),
            models.Index(fields=['status']),
            models.Index(fields=['snapshot_id', 'country_code', 'subdivision']),
        ]
        verbose_name = 'Partition Registry Entry'
        verbose_name_plural = 'Partition Registry'

    def mark_running(self):
        """Mark partition as running."""
        from django.utils import timezone
        self.status = self.PartitionStatus.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=['status', 'started_at', 'updated_at'])

    def mark_complete(self, entity_count: int = None, embedding_count: int = None):
        """Mark partition as complete with counts."""
        from django.utils import timezone
        self.status = self.PartitionStatus.COMPLETE
        self.completed_at = timezone.now()
        if entity_count is not None:
            self.entity_count = entity_count
        if embedding_count is not None:
            self.embedding_count = embedding_count
        self.save(update_fields=['status', 'completed_at', 'entity_count', 'embedding_count', 'updated_at'])

    def mark_failed(self, error_message: str):
        """Mark partition as failed with error details."""
        from django.utils import timezone
        self.status = self.PartitionStatus.FAILED
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save(update_fields=['status', 'completed_at', 'error_message', 'updated_at'])

    @classmethod
    def is_complete(cls, snapshot_id: str, country_code: str, subdivision: str = None) -> bool:
        """Check if a partition is already complete (idempotency gate)."""
        return cls.objects.filter(
            snapshot_id=snapshot_id,
            country_code=country_code,
            subdivision=subdivision,
            status=cls.PartitionStatus.COMPLETE,
        ).exists()

    @classmethod
    def get_or_create_pending(cls, snapshot_id: str, country_code: str, subdivision: str = None) -> 'PartitionRegistry':
        """Get existing pending/running partition or create a new one.

        Returns a tuple of ``(instance, created)`` consistent with Django's
        ``get_or_create`` semantics. If the partition already exists with
        status ``complete`` or ``failed``, it is returned as-is — the caller
        must check the status before proceeding.
        """
        obj, created = cls.objects.get_or_create(
            snapshot_id=snapshot_id,
            country_code=country_code,
            subdivision=subdivision,
            defaults={'status': cls.PartitionStatus.PENDING},
        )
        return obj, created

    def __str__(self):
        parts = [self.snapshot_id, self.country_code]
        if self.subdivision:
            parts.append(self.subdivision)
        return f"PartitionRegistry({'/'.join(parts)}) [{self.status}]"


class ReasoningWorkflow(models.Model):
    """Stores the agent's proposed GeoFlow Graph and its lifecycle.

    The agentic application layer (see ``docs/plans/GEO_SPATIAL_AGENT_CELERY_PLAN_V3.md``)
    proposes a DAG of operators to answer a natural-language question. This
    model records that proposal and tracks it through approval and execution.

    ``geo_flow_graph`` is the proposed operator DAG (a list of
    ``OperatorSignature``-shaped dicts, v3 §2/§5). ``status`` moves through
    ``proposed -> approved|rejected -> executed``.
    """

    class WorkflowStatus(models.TextChoices):
        PROPOSED = 'proposed', 'Proposed'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        EXECUTED = 'executed', 'Executed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.TextField()
    geo_flow_graph = models.JSONField(
        default=dict,
        help_text="The proposed operator DAG (list of OperatorSignature-shaped dicts).",
    )
    status = models.CharField(
        max_length=16, choices=WorkflowStatus.choices,
        default=WorkflowStatus.PROPOSED, db_index=True,
    )
    pipeline_run = models.ForeignKey(
        'core.PipelineRun', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='reasoning_workflows',
        help_text="Set once the workflow is executed (the dispatched Celery chain's run).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'reasoning_workflows'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['pipeline_run']),
        ]

    def mark_approved(self):
        self.status = self.WorkflowStatus.APPROVED
        self.approved_at = timezone.now()
        self.save(update_fields=['status', 'approved_at'])

    def mark_rejected(self):
        self.status = self.WorkflowStatus.REJECTED
        self.save(update_fields=['status'])

    def mark_executed(self, pipeline_run=None):
        self.status = self.WorkflowStatus.EXECUTED
        if pipeline_run is not None:
            self.pipeline_run = pipeline_run
        self.save(update_fields=['status', 'pipeline_run'])

    def __str__(self):
        return f"ReasoningWorkflow({self.status}) {self.question[:60]}"


class ReasoningStep(models.Model):
    """Stores each cognitive step (LLM/operator call) linked to the spatial
    artifacts it used.

    ``envelope`` stores the serialized ``OperatorEnvelope.to_dict()`` (v3 §2.3),
    not the legacy mutable ``OperatorConfig``. ``concept`` is a ``LatentSpace``
    enum value (v3: was ``CoreConcept``). ``artifacts`` is a JSON list of
    ``PipelineAsset.id`` UUIDs — the actual model name is ``PipelineAsset``
    (the plan referred to it as ``PipelineArtifact``).
    """

    class Feedback(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        EDITED = 'edited', 'Edited'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(
        ReasoningWorkflow, on_delete=models.CASCADE, related_name='steps',
    )
    step_index = models.IntegerField()
    concept = models.CharField(
        max_length=32,
        help_text="LatentSpace enum value (semantic/geographic/ontological/topological/spectral/temporal).",
    )
    role = models.CharField(
        max_length=32,
        help_text="FunctionalRole IntEnum value (SUBCOND/COND/SUPPORT/MEASURE).",
    )
    operator = models.CharField(max_length=64)
    envelope = models.JSONField(
        default=dict,
        help_text="Frozen, serializable OperatorEnvelope.to_dict() (v3 §2.3).",
    )
    prompt = models.TextField(blank=True, default='')
    response = models.TextField(blank=True, default='')
    artifacts = models.JSONField(
        default=list, blank=True,
        help_text="List of PipelineAsset.id UUIDs used/produced by this step.",
    )
    result = models.JSONField(default=dict, blank=True)
    feedback = models.CharField(
        max_length=16, choices=Feedback.choices, default=Feedback.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'reasoning_steps'
        ordering = ['workflow', 'step_index']
        unique_together = [('workflow', 'step_index')]
        indexes = [
            models.Index(fields=['workflow', 'step_index']),
            models.Index(fields=['concept']),
            models.Index(fields=['feedback']),
        ]

    def __str__(self):
        return f"ReasoningStep({self.workflow_id}, #{self.step_index}) {self.operator}"


# ══════════════════════════════════════════════════════════════════════════
# SnapshotJob — DB ground truth for "has this country been processed for
# this snapshot date?" Replaces the in-memory runsByYear tracking and the
# filesystem-based SnapshotDatesView scanning. See
# docs/plans/TEMPORAL_SNAPSHOT_REFACTOR.md.
# ══════════════════════════════════════════════════════════════════════════
