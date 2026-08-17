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
    
    pbf_file = models.ForeignKey('core.PbfFile', on_delete=models.CASCADE)
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
        'core.PbfFile',
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


class CountrySearchProcessing(models.Model):
    """
    Track search update processing status per country.
    Prevents regeneration and tracks pipeline progress.
    """
    
    country_name = models.CharField(max_length=100, unique=True, db_index=True)
    region_pbf = models.ForeignKey(
        'core.PbfFile', 
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


