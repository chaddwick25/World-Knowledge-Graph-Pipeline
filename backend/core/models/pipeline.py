from django.db import models
import uuid
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django_prometheus.models import ExportModelOperationsMixin



class PipelineRun(models.Model):
    """
    Track complete pipeline runs with stage-level state tracking.
    References OSMWikiDataHierarchy as configuration source.
    """
    
    class PipelineStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Configuration reference
    osm_wikidata_hierarchy = models.ForeignKey(
        'core.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pipeline_runs',
        help_text="Configuration source from OSM/Wikidata"
    )
    
    country_code = models.CharField(
        max_length=3,
        db_index=True,
        help_text="ISO 3166-1 alpha-3 country code"
    )
    country_name = models.CharField(max_length=100)
    
    pipeline_type = models.CharField(
        max_length=50,
        default='worldkg_unified',
        help_text="Pipeline type (e.g., 'worldkg_unified', 'subgraph_encoder')"
    )
    
    status = models.CharField(
        max_length=20,
        choices=PipelineStatus.choices,
        default=PipelineStatus.PENDING
    )
    
    configuration = models.JSONField(default=dict)
    
    # Stage tracking
    current_stage = models.CharField(max_length=50, null=True, blank=True)
    completed_stages = ArrayField(
        models.CharField(max_length=50),
        default=list,
        blank=True
    )
    
    # Metrics per stage
    stage_metrics = models.JSONField(
        default=dict,
        help_text="Metrics tracked per pipeline stage"
    )
    
    subgraph_task_ids = models.JSONField(
        default=dict,
        blank=True,
        help_text="Celery task IDs per subgraph: {subgraph_slug: task_id}"
    )

    results = models.JSONField(default=dict)
    error_message = models.TextField(null=True, blank=True)
    
    queued_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the Celery task was queued (after apply_async)"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    total_entities_processed = models.BigIntegerField(default=0)
    total_entities_aligned = models.BigIntegerField(default=0)
    total_spatial_links = models.BigIntegerField(default=0)
    
    processing_session = models.ForeignKey(
        'core.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'pipeline_runs'
        indexes = [
            models.Index(fields=['country_code', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['pipeline_type']),
            models.Index(fields=['osm_wikidata_hierarchy']),
        ]
        ordering = ['-created_at']
    
    def start_stage(self, stage_name):
        """Mark a stage as started."""
        self.current_stage = stage_name
        self.status = self.PipelineStatus.RUNNING
        if not self.started_at:
            self.started_at = timezone.now()
        self.save()
    
    def complete_stage(self, stage_name, metrics=None):
        """Mark a stage as completed."""
        if stage_name not in self.completed_stages:
            self.completed_stages.append(stage_name)
        
        if metrics:
            self.stage_metrics[stage_name] = metrics
        
        self.save(update_fields=['completed_stages', 'stage_metrics'])
    
    def mark_completed(self, results=None):
        """Mark the entire pipeline run as completed."""
        self.status = self.PipelineStatus.COMPLETED
        self.completed_at = timezone.now()
        self.current_stage = None
        if results:
            self.results = results
        self.save()
    
    def mark_failed(self, error_message):
        """Mark the pipeline run as failed."""
        self.status = self.PipelineStatus.FAILED
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.current_stage = None
        self.save()
    
    def __str__(self):
        return f"{self.country_name} - {self.pipeline_type} ({self.get_status_display()})"


class PipelineLogEntry(models.Model):
    """Structured, queryable log entry for a pipeline run.

    Replaces the file-based WatchedFileHandler logging. Each entry is a
    single log line (step start, step complete, warning, error) with
    optional structured metadata.

    When DB sharding lands, this model routes to the shard DB via
    StorageTarget.db_alias (see ShardRouter in database_router.py).
    """

    class Level(models.TextChoices):
        DEBUG = 'DEBUG', 'Debug'
        INFO = 'INFO', 'Info'
        WARNING = 'WARNING', 'Warning'
        ERROR = 'ERROR', 'Error'
        CRITICAL = 'CRITICAL', 'Critical'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # FK is named `run` (auto-column `run_id`) so the denormalized
    # `pipeline_run_id` UUID field below can coexist for shard routing.
    run = models.ForeignKey(
        PipelineRun,
        on_delete=models.CASCADE,
        related_name='log_entries',
        null=True,
        blank=True,
        db_index=True,
    )
    pipeline_run_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="Denormalized for shard routing without JOIN",
    )
    # Step context
    step_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    step_index = models.FloatField(null=True, blank=True)
    # Country context (denormalized for shard routing)
    country_code = models.CharField(max_length=3, null=True, blank=True, db_index=True)
    continent = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    # Log content
    level = models.CharField(
        max_length=10, choices=Level.choices, default=Level.INFO, db_index=True
    )
    message = models.TextField()
    metadata = models.JSONField(
        default=dict, blank=True,
        help_text="Structured metadata: duration_ms, entity_count, error_traceback, etc."
    )
    # Celery task linkage
    task_id = models.CharField(
        max_length=255, null=True, blank=True, db_index=True,
        help_text="Celery task ID (links to TaskResult.task_id)"
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'pipeline_log_entries'
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['pipeline_run_id', 'timestamp']),
            models.Index(fields=['country_code', 'timestamp']),
            models.Index(fields=['level', 'timestamp']),
            models.Index(fields=['step_name', 'timestamp']),
        ]

    def __str__(self):
        return f"[{self.level}] {self.step_name or '-'}: {self.message[:80]}"


class PipelineAsset(models.Model):
    """
    Track assets generated during pipeline execution with lineage.
    Preserves process proximity - each asset links to its source.
    """
    
    class AssetType(models.TextChoices):
        PBF_FILE = 'PBF_FILE', 'PBF File'
        GV_TAGS_EMBEDDING = 'GV_TAGS_EMBEDDING', 'GV-Tags Embedding'
        GV_NLE_EMBEDDING = 'GV_NLE_EMBEDDING', 'GV-NLE Embedding'
        WIKIDATA_CANDIDATES = 'WIKIDATA_CANDIDATES', 'Wikidata Candidates'
        IGEA_ALIGNMENT = 'IGEA_ALIGNMENT', 'IGEA Alignment'
        SPATIAL_LINKS = 'SPATIAL_LINKS', 'Spatial Links'
        COUNTRY_PICKLE = 'COUNTRY_PICKLE', 'Country Pickle'
        SUBGRAPH_PICKLE = 'SUBGRAPH_PICKLE', 'Subgraph Pickle'
        METRICS = 'METRICS', 'Metrics'
        OTHER = 'OTHER', 'Other'
    
    class AssetStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        GENERATING = 'GENERATING', 'Generating'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    pipeline_run = models.ForeignKey(
        PipelineRun,
        on_delete=models.CASCADE,
        related_name='assets'
    )
    
    asset_type = models.CharField(max_length=30, choices=AssetType.choices)
    asset_name = models.CharField(max_length=200)
    stage_name = models.CharField(max_length=50)
    
    storage_type = models.CharField(max_length=20, default='filesystem')
    storage_path = models.TextField()
    
    status = models.CharField(
        max_length=20,
        choices=AssetStatus.choices,
        default=AssetStatus.PENDING
    )
    
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    record_count = models.BigIntegerField(null=True, blank=True)
    
    # Lineage tracking for process proximity
    parent_asset = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_assets'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    metadata = models.JSONField(default=dict)

    # Denormalized for ShardRouter routing (same pattern as PipelineLogEntry).
    # Backfilled from PipelineRun.country_code -> CountryPipelineProfile ->
    # country_relations_payload["continent"]. Lets ShardRouter route writes to
    # the continent-scoped shard DB without a JOIN to PipelineRun.
    continent = models.CharField(
        max_length=50, null=True, blank=True, db_index=True,
        help_text="Denormalized for shard routing without JOIN",
    )

    class Meta:
        db_table = 'pipeline_assets'
        indexes = [
            models.Index(fields=['pipeline_run', 'asset_type']),
            models.Index(fields=['pipeline_run', 'stage_name']),
            models.Index(fields=['status']),
            models.Index(fields=['continent', 'asset_type']),
        ]
        ordering = ['pipeline_run', 'stage_name', 'asset_type']

    def __str__(self):
        return f"{self.asset_name} ({self.get_asset_type_display()})"


# SnapshotJob has been moved to osmsnapshot.models.SnapshotJob.
# The DB table 'snapshot_jobs' is unchanged; only the Django app label changed
# from 'orchestration' to 'osmsnapshot'. See osmsnapshot/migrations/0002_*.py.
