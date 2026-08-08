from django.db import models
import uuid
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django_prometheus.models import ExportModelOperationsMixin
from prometheus_client import Summary


processing_duration = Summary(
    'processing_session_duration_seconds',
    'Duration of processing sessions in seconds',
    ['session_type', 'status']
)


class ProcessingSession(ExportModelOperationsMixin('processing_session'), models.Model):
    """Track processing sessions for dataset generation and analysis"""
    
    class SessionType(models.TextChoices):
        DATASET_GENERATION = 'DATASET_GENERATION', 'Dataset Generation'
        METRICS_ANALYSIS = 'METRICS_ANALYSIS', 'Metrics Analysis'
        TEMPORAL_CORPUS = 'TEMPORAL_CORPUS', 'Temporal Corpus Creation'
        GEOGRAPHIC_EXTRACT = 'GEOGRAPHIC_EXTRACT', 'Geographic Extract'
        QUICK_TRAINING = 'QUICK_TRAINING', 'Quick Training'
        TREND_ANALYSIS = 'TREND_ANALYSIS', 'Trend Analysis'
        TAG_DISCOVERY = 'TAG_DISCOVERY', 'Tag Discovery'
        SEARCH_UPDATE = 'SEARCH_UPDATE', 'Search Update'
        GEOVECTORS_ENCODING = 'GEOVECTORS_ENCODING', 'GeoVectors Encoding'
    
    class SessionStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
    
    class AnalysisStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ANALYZING = 'ANALYZING', 'Analyzing'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_name = models.CharField(max_length=200)
    session_type = models.CharField(max_length=30, choices=SessionType.choices)
    status = models.CharField(max_length=20, choices=SessionStatus.choices, default=SessionStatus.PENDING)
    configuration = models.JSONField(default=dict, blank=True)
    results = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    statistical_analysis_status = models.CharField(
        max_length=20,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.PENDING,
        help_text="Status of statistical analysis"
    )
    statistical_analysis_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When statistical analysis completed"
    )
    
    svd_analysis_status = models.CharField(
        max_length=20,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.PENDING,
        help_text="Status of SVD analysis"
    )
    svd_analysis_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When SVD analysis completed"
    )
    
    structural_tags_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Top 10 structural tags for quick access"
    )
    temporal_tags_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Top 10 temporal tags for quick access"
    )
    anchor_nodes_summary = models.JSONField(
        null=True,
        blank=True,
        help_text="Top 10 anchor nodes for quick access"
    )
    
    analysis_metadata = models.JSONField(
        null=True,
        blank=True,
        help_text="SVD metadata: n_components, explained_variance, etc."
    )
    
    def mark_completed(self, results: dict = None):
        """Mark session as completed with optional results"""
        from django.utils import timezone
        self.status = self.SessionStatus.COMPLETED
        self.completed_at = timezone.now()
        if results:
            self.results = results
        self.save()
    
    def __str__(self):
        return f"Session {self.session_name} ({self.get_session_type_display()}) - {self.get_status_display()}"


@receiver(post_save, sender=ProcessingSession)
def update_processing_metrics(sender, instance, **kwargs):
    """Update Prometheus metrics when a session is completed or failed."""
    if instance.status in [ProcessingSession.SessionStatus.COMPLETED, ProcessingSession.SessionStatus.FAILED]:
        if instance.completed_at and instance.created_at:
            duration = (instance.completed_at - instance.created_at).total_seconds()
            processing_duration.labels(
                session_type=instance.session_type,
                status=instance.status
            ).observe(duration)


class Task(models.Model):
    class TaskType(models.TextChoices):
        DOWNLOAD_PBF = 'DOWNLOAD_PBF', 'Download PBF File'
        EXTRACT_PBF = 'EXTRACT_PBF', 'Extract PBF from History'
        CREATE_SNAPSHOT = 'CREATE_SNAPSHOT', 'Create Snapshot'
        GENERATE_DATASET = 'GENERATE_DATASET', 'Generate Dataset'
        ANALYZE_METRICS = 'ANALYZE_METRICS', 'Analyze Metrics'
        WORLDKG_PIPELINE = 'WORLDKG_PIPELINE', 'WorldKG Full Pipeline'
        GEOVECTORS_ENCODE = 'GEOVECTORS_ENCODE', 'GeoVectors Encode'
        HARVEST_IGEA = 'HARVEST_IGEA', 'Harvest + IGEA'
        PREDICT_LINKS = 'PREDICT_LINKS', 'Predict Spatial Links'

    class TaskStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING)
    parameters = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    processing_session = models.ForeignKey(ProcessingSession, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Task {self.id} ({self.get_task_type_display()}) - {self.get_status_display()}"


class OsmiumDatasetMetrics(models.Model):
    """Store comprehensive osmium-generated dataset metrics"""
    
    pbf_file = models.ForeignKey('extraction.PbfFile', on_delete=models.CASCADE)
    processing_session = models.ForeignKey(ProcessingSession, on_delete=models.CASCADE)
    
    total_nodes = models.BigIntegerField(default=0)
    total_ways = models.BigIntegerField(default=0) 
    total_relations = models.BigIntegerField(default=0)
    file_size_bytes = models.BigIntegerField(default=0)
    
    unique_tag_count = models.IntegerField(default=0)
    tag_distribution = models.JSONField(default=dict)
    tag_entropy = models.FloatField(null=True, blank=True)
    most_common_tags = models.JSONField(default=dict)
    
    bounding_box = models.JSONField(default=dict)
    geographic_coverage_km2 = models.FloatField(null=True, blank=True)
    coordinate_precision = models.FloatField(null=True, blank=True)
    
    referential_integrity_score = models.FloatField(null=True, blank=True)
    missing_references_count = models.IntegerField(default=0)
    data_completeness_score = models.FloatField(null=True, blank=True)
    overall_quality_score = models.FloatField(null=True, blank=True)
    
    temporal_coverage_start = models.DateTimeField(null=True, blank=True)
    temporal_coverage_end = models.DateTimeField(null=True, blank=True)
    temporal_resolution_days = models.FloatField(null=True, blank=True)
    
    metrics_generated_at = models.DateTimeField(auto_now_add=True)
    osmium_version = models.CharField(max_length=50, blank=True, default='')
    processing_duration_seconds = models.FloatField(null=True, blank=True)
    
    def __str__(self):
        return f"Metrics for {self.pbf_file} - Session {self.processing_session.session_name}"


class RegisteredService(models.Model):
    """
    Represents a service that can be executed via the API.
    This model acts as a registry for available operations.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    name = models.CharField(
        max_length=100, 
        unique=True, 
        db_index=True,
        help_text="Unique identifier for the service used in the API URL."
    )
    
    service_target = models.CharField(
        max_length=255,
        help_text="Full Python path to the service method (e.g., 'module.submodule.ClassName.method_name')."
    )
    
    description = models.TextField(
        blank=True,
        help_text="A brief description of the service's function for display in the UI."
    )
    
    config_schema = models.JSONField(
        default=dict, 
        blank=True,
        help_text="JSON schema for validating the runtime configuration object."
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Whether the service is active and available for execution."
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['name']
        db_table = 'registered_services'
        
    def __str__(self):
        return self.name


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
    
    # TODO: Implement and test Cloud Storage Backup metadata
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
        ProcessingSession,
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


class CountrySearchProcessing(models.Model):
    """
    Track search update processing status per country.
    Prevents regeneration and tracks pipeline progress.
    """
    
    country_name = models.CharField(max_length=100, unique=True, db_index=True)
    region_pbf = models.ForeignKey(
        'extraction.PbfFile', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='country_region'
    )
    # Processing status
    is_processed = models.BooleanField(default=False, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    # Extraction counts
    yearly_extracts_count = models.IntegerField(default=0)
    monthly_extracts_count = models.IntegerField(default=0)
    asset_bundles_count = models.IntegerField(default=0)
    # Temporal range
    temporal_start = models.DateField(null=True, blank=True)
    temporal_end = models.DateField(null=True, blank=True)
    processing_session = models.ForeignKey(
        ProcessingSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'country_search_processing'
        ordering = ['country_name']
    
    def __str__(self):
        status = 'Processed' if self.is_processed else 'Pending'
        return f"{self.country_name} - {status}"


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
        'extraction.OSMWikiDataHierarchy',
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
        ProcessingSession,
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


class PlanetSnapshot(models.Model):
    """Represents a single planet snapshot run.

    Anchors a specific planet .osm.pbf file and versions the spatial hierarchy
    for downstream continent/country/subgraph processing.
    """

    class SnapshotStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Phase 1: Temporal sharding support ──
    snapshot_date_str = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31')",
    )

    snapshot_date = models.DateField(db_index=True)
    planet_osm_path = models.CharField(
        max_length=1024,
        help_text="Absolute path to the planet .osm.pbf used for this snapshot.",
    )
    planet_pbf = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='planet_snapshots',
        help_text="PbfFile row representing the planet PBF in HOT storage.",
    )

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.BigIntegerField(null=True, blank=True, help_text="OSM nodes in planet")
    way_count = models.BigIntegerField(null=True, blank=True, help_text="OSM ways in planet")
    relation_count = models.BigIntegerField(null=True, blank=True, help_text="OSM relations in planet")
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text="Planet PBF file size")

    status = models.CharField(
        max_length=20,
        choices=SnapshotStatus.choices,
        default=SnapshotStatus.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'planet_snapshots'
        ordering = ['-snapshot_date']
        indexes = [
            models.Index(fields=['snapshot_date']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"PlanetSnapshot {self.snapshot_date}"


class CountryRelationSnapshot(models.Model):
    """Snapshot of legacy country_relations.json for a given planet snapshot.

    One row per (planet_snapshot, ISO code). This provides a relational view of
    the JSON used during initialization, suitable for visualization and
    inspecting OSM/Wikidata/Geofabrik metadata alongside the hierarchy models.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    planet_snapshot = models.ForeignKey(
        PlanetSnapshot,
        on_delete=models.CASCADE,
        related_name="country_relations",
    )

    # Identity
    iso_code = models.CharField(max_length=3, db_index=True)
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=255, db_index=True)

    # Hierarchy
    parent_slug = models.CharField(max_length=255, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    continent_id = models.UUIDField(null=True, blank=True)

    # OSM / Wikidata / Geofabrik
    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    geofabrik_pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    # GeoVectors TSV paths (if present in the JSON overlay)
    geovectors_location_tsv = models.CharField(max_length=1024, null=True, blank=True)
    geovectors_tags_tsv = models.CharField(max_length=1024, null=True, blank=True)

    # Raw JSON payload for forward-compatibility / auditing
    raw_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "country_relation_snapshots"
        unique_together = ("planet_snapshot", "iso_code")
        indexes = [
            models.Index(fields=["planet_snapshot", "iso_code"]),
            models.Index(fields=["continent_name"]),
        ]

    def __str__(self):
        return f"CountryRelationSnapshot({self.iso_code}, {self.name})"


class ContinentProfile(models.Model):
    """One row per (planet_snapshot, continent) for spatial preprocessing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    planet_snapshot = models.ForeignKey(
        PlanetSnapshot,
        on_delete=models.CASCADE,
        related_name='continents',
    )

    slug = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=255, null=True, blank=True)

    geofabrik_slug = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    regional_state = models.ForeignKey(
        'extraction.RegionalExtractionState',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )
    continent_pbf = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )
    continent_poly = models.ForeignKey(
        'extraction.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.BigIntegerField(null=True, blank=True, help_text='OSM nodes in continent extract')
    way_count = models.BigIntegerField(null=True, blank=True, help_text='OSM ways in continent extract')
    relation_count = models.BigIntegerField(null=True, blank=True, help_text='OSM relations in continent extract')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Continent PBF file size')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'continent_profiles'
        unique_together = ('planet_snapshot', 'slug')
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['planet_snapshot', 'slug']),
        ]

    def __str__(self):
        return f"{self.slug} ({self.planet_snapshot.snapshot_date})"


class CountryPipelineProfile(models.Model):
    """Canonical, embedding-driven country configuration.

    One row per country that has embeddings available in cold storage.
    """

    class MetadataStatus(models.TextChoices):
        OK = 'OK', 'OK'
        MISSING_GEOFABRIK = 'MISSING_GEOFABRIK', 'Missing Geofabrik metadata'
        MISSING_WIKIDATA = 'MISSING_WIKIDATA', 'Missing Wikidata hierarchy'
        ORPHAN_EMBEDDINGS = 'ORPHAN_EMBEDDINGS', 'Embeddings without hierarchy metadata'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs manual review'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Hierarchy anchors
    planet_snapshot = models.ForeignKey(
        PlanetSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    continent_profile = models.ForeignKey(
        ContinentProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )

    # Identity
    iso2 = models.CharField(max_length=2, null=True, blank=True, db_index=True)
    iso3 = models.CharField(max_length=3, null=True, blank=True, db_index=True)
    canonical_name = models.CharField(max_length=255)
    canonical_slug = models.CharField(max_length=255, db_index=True)

    # Embedding side
    embedding_slug = models.CharField(
        max_length=255,
        unique=True,
        help_text="Directory or file slug under EMBEDDINGS_ROOT used for this country's embeddings.",
    )
    embedding_root_path = models.CharField(
        max_length=1024,
        help_text="Relative path from EMBEDDINGS_ROOT to this country's embedding directory.",
    )
    has_embeddings = models.BooleanField(default=True, db_index=True)

    # Overrides (from overrides.json)
    has_override = models.BooleanField(default=False, db_index=True)
    override_country_slug = models.CharField(max_length=255, null=True, blank=True)
    override_poly_slug = models.CharField(max_length=255, null=True, blank=True)
    override_embedding_slug = models.CharField(max_length=255, null=True, blank=True)
    override_source = models.CharField(max_length=50, null=True, blank=True)
    overrides_synced_at = models.DateTimeField(null=True, blank=True)
    raw_override_record = models.JSONField(default=dict, blank=True)

    # Pre-computed canonical paths (set by prebuild_country_paths command)
    embedding_location_tsv_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to locations.tsv.gz under EMBEDDINGS_ROOT",
    )
    embedding_tags_tsv_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to tags.tsv.gz under EMBEDDINGS_ROOT",
    )
    snapshot_pbf_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the temporal snapshot .osm.pbf",
    )
    snapshot_poly_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the temporal snapshot .osm.poly",
    )
    pickle_dir = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the pickle directory for wdw.pickle",
    )
    continent_pbf_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the continent .pbf this country is extracted from",
    )

    # OSM / Wikidata / Geofabrik side
    osm_wikidata_hierarchy = models.ForeignKey(
        'extraction.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
        help_text="Linked country-level OSM/Wikidata hierarchy (admin_level=2).",
    )

    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_id = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    continent_id = models.UUIDField(null=True, blank=True)

    geofabrik_slug = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    geofabrik_parent_slug = models.CharField(max_length=255, null=True, blank=True)
    geofabrik_pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    regional_state = models.ForeignKey(
        'extraction.RegionalExtractionState',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    country_pbf = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    country_poly = models.ForeignKey(
        'extraction.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.IntegerField(null=True, blank=True, help_text='OSM nodes in country extract')
    way_count = models.IntegerField(null=True, blank=True, help_text='OSM ways in country extract')
    relation_count = models.IntegerField(null=True, blank=True, help_text='OSM relations in country extract')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Country PBF file size')
    country_relations_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional snapshot of legacy country_relations.json entry for this country.",
    )
    # Subgraph gate
    has_subgraphs = models.BooleanField(default=False, db_index=True)
    # ── Temporal sharding (Phase 1+: snapshot-aware paths) ──
    snapshot_date = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        db_index=True,
        help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31'). "
                  "When set, paths resolve under continents/{snapshot_date}/.",
    )
    # Status / diagnostics
    # TODO: I am going to need  change the shape of this field once we fixed the temporal snapshot issues 
    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.OK,
        db_index=True,
    )
    # TODO: Remove this feild
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        db_table = 'country_pipeline_profiles'
        indexes = [
            models.Index(fields=['iso3']),
            models.Index(fields=['canonical_slug']),
            models.Index(fields=['metadata_status']),
            models.Index(fields=['has_subgraphs']),
        ]

    def __str__(self):
        code = self.iso3 or self.iso2 or self.embedding_slug
        return f"{self.canonical_name} ({code})"


class SubgraphProfile(models.Model):
    """Canonical subgraph (admin region / city) configuration per country.

    Links to CountryPipelineProfile and, where possible, to OSM/Wikidata hierarchy
    and PolygonFile / subgraph artifacts.
    """

    class MetadataStatus(models.TextChoices):
        OK = 'OK', 'OK'
        ORPHAN_POLY = 'ORPHAN_POLY', 'Polygon without hierarchy metadata'
        MISSING_RELATION = 'MISSING_RELATION', 'Missing OSM relation'
        NO_PICKLE = 'NO_PICKLE', 'Subgraph pickle missing'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs manual review'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_profile = models.ForeignKey(
        CountryPipelineProfile,
        on_delete=models.CASCADE,
        related_name='subgraphs',
    )
    # Identity
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=255)
    # Hierarchy / Wikidata
    osm_wikidata_hierarchy = models.ForeignKey(
        'extraction.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
        help_text="Linked subgraph-level hierarchy (e.g., admin_level 4/6).",
    )
    admin_level = models.IntegerField(null=True, blank=True, db_index=True)
    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_id = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Polygon / filesystem linkage
    polygon_file = models.ForeignKey(
        'extraction.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
    )
    subgraph_pbf = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
    )
    polygon_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_pbf_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_poly_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_pickle_path = models.CharField(max_length=1024, null=True, blank=True)

    bbox_min_lon = models.FloatField(null=True, blank=True)
    bbox_min_lat = models.FloatField(null=True, blank=True)
    bbox_max_lon = models.FloatField(null=True, blank=True)
    bbox_max_lat = models.FloatField(null=True, blank=True)

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.IntegerField(null=True, blank=True, help_text='OSM nodes in subgraph')
    way_count = models.IntegerField(null=True, blank=True, help_text='OSM ways in subgraph')
    relation_count = models.IntegerField(null=True, blank=True, help_text='OSM relations in subgraph')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Subgraph PBF file size')

    # Status / diagnostics
    has_subgraph_pbf = models.BooleanField(default=False)
    has_subgraph_poly = models.BooleanField(default=False)
    has_subgraph_pickle = models.BooleanField(default=False)

    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.OK,
        db_index=True,
    )
    notes = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'subgraph_profiles'
        unique_together = ('country_profile', 'slug')
        indexes = [
            models.Index(fields=['country_profile', 'slug']),
            models.Index(fields=['admin_level']),
            models.Index(fields=['osm_relation_id']),
            models.Index(fields=['metadata_status']),
        ]

    def __str__(self):
        return f"{self.country_profile.canonical_name} / {self.name}"


# ══════════════════════════════════════════════════════════════════════════
# EligibleCountry (Init Pipeline eligibility & readiness)
# ══════════════════════════════════════════════════════════════════════════

# TODO: I feel like this could be a computed property
class EligibleCountry(models.Model):
    """Tracks which countries are eligible for the pipeline per snapshot.

    Populated by the ``scan_embeddings`` management command.  Drives the map
    colouring (green READY / yellow NEEDS_SPLIT / orange NEEDS_MERGE / grey
    NO_EMBEDDINGS) and the init pipeline country-selection UI.

    One row per ``(country_name, snapshot_date)`` pair.  ``country_name`` is
    the human-readable name (e.g. ``"Scotland"``, ``"France"``) — not a slug
    or ISO code — because the frontend uses display names everywhere.
    """

    class EmbeddingStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending Scan'
        READY = 'READY', 'Embeddings Ready'
        NEEDS_SPLIT = 'NEEDS_SPLIT', 'Needs TSV Splitting'
        NEEDS_MERGE = 'NEEDS_MERGE', 'Needs TSV Merging'
        NO_EMBEDDINGS = 'NO_EMBEDDINGS', 'No Embeddings Found'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Identity — loosely coupled; no FK to CountryPipelineProfile
    # because countries may be eligible (or pending scan) before a profile exists.
    country_name = models.CharField(max_length=255, db_index=True)
    iso_code = models.CharField(max_length=10, null=True, blank=True, db_index=True)
    continent = models.CharField(max_length=100)
    # Snapshot context
    snapshot_date = models.CharField(
        max_length=10, default='2025_12_31', db_index=True,
        help_text='YYYY_MM_DD format snapshot date',
    )
    # Embedding status
    embedding_status = models.CharField(
        max_length=20, choices=EmbeddingStatus.choices,
        default=EmbeddingStatus.PENDING,
    )

    # Discovered paths (from scan)
    location_tsv_path = models.CharField(max_length=1024, null=True, blank=True)
    tags_tsv_path = models.CharField(max_length=1024, null=True, blank=True)
    pickle_path = models.CharField(max_length=1024, null=True, blank=True)

    # Split/merge metadata — populated only when NEEDS_SPLIT or NEEDS_MERGE
    source_tsv_name = models.CharField(
        max_length=255, null=True, blank=True,
        help_text='If NEEDS_SPLIT, parent multi-country TSV name (e.g. great-britain-location)',
    )
    source_continent = models.CharField(
        max_length=100, null=True, blank=True,
        help_text='If NEEDS_SPLIT, continent of the source TSV directory',
    )
    needs_merge_regions = ArrayField(
        models.CharField(max_length=100), default=list, blank=True,
        help_text='If NEEDS_MERGE, region slugs to concatenate (e.g. [us-midwest, us-northeast, …])',
    )

    # Init pipeline state
    is_initialized = models.BooleanField(default=False)
    initialized_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    scanned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'eligible_countries'
        unique_together = ('country_name', 'snapshot_date')
        indexes = [
            models.Index(fields=['embedding_status']),
            models.Index(fields=['is_initialized', 'snapshot_date']),
            models.Index(fields=['continent', 'snapshot_date']),
        ]
        verbose_name = 'Eligible Country'
        verbose_name_plural = 'Eligible Countries'

    def __str__(self):
        return f'{self.country_name} ({self.snapshot_date}) [{self.embedding_status}]'


# ══════════════════════════════════════════════════════════════════════════
# PartitionRegistry (Temporal Sharding Phase 0)
# ══════════════════════════════════════════════════════════════════════════


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
        PipelineRun, null=True, blank=True, on_delete=models.SET_NULL,
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
class SnapshotJob(models.Model):
    """DB-backed record of a (snapshot_date, country_code) processing job.

    The ``unique_together`` constraint enforces "once processed, can't
    re-run" at the DB level. The frontend queries this model (via the
    ``/api/snapshot-jobs/`` endpoints) to populate the year selector with
    per-year status badges instead of relying on the in-memory
    ``runsByYear`` Set that was lost on page refresh.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot_date = models.CharField(
        max_length=10, db_index=True,
        help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31').",
    )
    country_code = models.CharField(
        max_length=3, db_index=True,
        help_text="ISO 3166-1 alpha-2 country code (e.g., 'JM').",
    )
    country_name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )

    # Link to Celery + pipeline tracking
    pipeline_run = models.ForeignKey(
        PipelineRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='snapshot_jobs',
    )
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    # Results summary (populated on Step 6 completion)
    total_entities = models.BigIntegerField(default=0)
    total_aligned = models.BigIntegerField(default=0)
    total_spatial_links = models.BigIntegerField(default=0)
    error_message = models.TextField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'snapshot_jobs'
        unique_together = [('snapshot_date', 'country_code')]
        indexes = [
            models.Index(fields=['snapshot_date', 'status']),
            models.Index(fields=['country_code', 'status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"SnapshotJob({self.country_code}, {self.snapshot_date}, {self.status})"

    def mark_running(self, pipeline_run=None, celery_task_id=None):
        """Transition to RUNNING and record the run/task links."""
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        if pipeline_run is not None:
            self.pipeline_run = pipeline_run
        if celery_task_id is not None:
            self.celery_task_id = celery_task_id
        self.save(update_fields=[
            'status', 'started_at', 'pipeline_run', 'celery_task_id',
        ])

    def mark_completed(self, total_entities=0, total_aligned=0,
                       total_spatial_links=0):
        """Transition to COMPLETED with result counts."""
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.total_entities = total_entities
        self.total_aligned = total_aligned
        self.total_spatial_links = total_spatial_links
        self.error_message = None
        self.save(update_fields=[
            'status', 'completed_at', 'total_entities', 'total_aligned',
            'total_spatial_links', 'error_message',
        ])

    def mark_failed(self, error_message):
        """Transition to FAILED with an error message."""
        self.status = self.Status.FAILED
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save(update_fields=[
            'status', 'completed_at', 'error_message',
        ])
