"""OSM Snapshot app — models for snapshot identity and graph data extraction.

A **snapshot** is a history-flattened OSM extract pinned to a moment in time
for a country. It is the unit of work for a pipeline run and the scoping key
for all downstream graph and drift analysis.

A **GraphExtract** is the graph substrate (nodes, edges, tags in Parquet)
extracted from a snapshot's PBF. It is the raw material from which road
network, spatial k-NN, and semantic graphs are derived.
"""

import uuid
from django.db import models
from django.utils import timezone


class Snapshot(models.Model):
    """A history-flattened OSM extract pinned to a moment in time for a country.

    Produced by ``osmium extract --polygon`` (country boundary from continent
    PBF) followed by ``osmium time-filter {date}`` (flatten history to a point
    in time). The PBF file itself is tracked via the ``pbf_file`` FK to
    ``extraction.PbfFile``.

    Identity: ``(country_code, snapshot_date)`` — one snapshot per country per
    date. No intervals, no filters; a snapshot is just a point in time.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_code = models.CharField(
        max_length=3, db_index=True,
        help_text="ISO 3166-1 alpha-2 country code (e.g., 'CA', 'JM').",
    )
    snapshot_date = models.CharField(
        max_length=10, db_index=True,
        help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31').",
    )
    pbf_file = models.ForeignKey(
        'core.PbfFile',
        on_delete=models.CASCADE,
        related_name='snapshots',
        help_text="The flattened (no-history) PBF file for this snapshot.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'osm_snapshots'
        unique_together = [('country_code', 'snapshot_date')]
        ordering = ['-snapshot_date']
        indexes = [
            models.Index(fields=['country_code', 'snapshot_date']),
        ]

    def __str__(self):
        return f"Snapshot({self.country_code}, {self.snapshot_date})"


class GraphExtract(models.Model):
    """Graph data (nodes, edges, tags) extracted from a snapshot's PBF.

    Stored as Parquet files in ``extract_path``:
    - ``nodes.parquet`` — ``(osm_id, lat, lon)``
    - ``edges.parquet`` — ``(way_id, osm_id_a, osm_id_b)``
    - ``tags.parquet``  — ``(osm_id, key, value)``

    This is the substrate from which multiple graphs are derived:
    - Road network graph (filter ``tags.key='highway'`` ∈ routable types)
    - Spatial k-NN graph (compute haversine neighbors on nodes)
    - Semantic graph (join ``wkg_class`` from enrichment)

    One GraphExtract per snapshot.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(
        Snapshot,
        on_delete=models.CASCADE,
        related_name='graph_extracts',
        help_text="The snapshot this graph data was extracted from.",
    )
    extract_path = models.CharField(
        max_length=1024,
        help_text="Directory path containing nodes.parquet, edges.parquet, tags.parquet.",
    )
    node_count = models.IntegerField(default=0)
    way_count = models.IntegerField(default=0)
    relation_count = models.IntegerField(default=0)
    edge_count = models.IntegerField(default=0)
    tag_count = models.IntegerField(default=0)
    total_size_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'graph_extracts'
        unique_together = [('snapshot',)]
        ordering = ['-created_at']

    def __str__(self):
        return f"GraphExtract({self.snapshot.country_code}, {self.snapshot.snapshot_date})"


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
        'core.PipelineRun', on_delete=models.SET_NULL, null=True, blank=True,
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


class PbfTagDistribution(models.Model):
    """Store individual tag distributions for PBF files."""

    pbf_file = models.ForeignKey(
        'core.PbfFile', on_delete=models.CASCADE,
        related_name='tag_distributions',
    )
    processing_session = models.ForeignKey(
        'core.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='tag_distributions',
        help_text="Pipeline session that produced this tag distribution",
    )
    tag_key = models.CharField(max_length=255, db_index=True)
    tag_value = models.CharField(max_length=255, db_index=True)
    count = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pbf_tag_distributions'
        unique_together = ['pbf_file', 'tag_key', 'tag_value']
        ordering = ['-count']
        indexes = [
            models.Index(fields=['tag_key', 'tag_value']),
            models.Index(fields=['pbf_file', 'count']),
        ]

    def __str__(self):
        return f"{self.tag_key}={self.tag_value}: {self.count} (PBF: {self.pbf_file.id})"

    @property
    def tag_full(self):
        """Return full tag as key=value"""
        return f"{self.tag_key}={self.tag_value}"


class WorldKGClassDrift(models.Model):
    """Tracks ontological class distribution drift over time for a region.

    Enables semantic change detection beyond raw embedding drift.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.CharField(max_length=255, db_index=True)
    bbox = models.JSONField(
        help_text="Bounding box as [min_lon, min_lat, max_lon, max_lat]"
    )

    snapshot_from = models.ForeignKey(
        'osmsnapshot.Snapshot',
        on_delete=models.CASCADE,
        related_name='wkg_drift_from',
        help_text="Earlier snapshot"
    )
    snapshot_to = models.ForeignKey(
        'osmsnapshot.Snapshot',
        on_delete=models.CASCADE,
        related_name='wkg_drift_to',
        help_text="Later snapshot"
    )

    # Class distribution snapshots
    class_distribution_from = models.JSONField(
        help_text="WorldKG class distribution at t0: {class: count}"
    )
    class_distribution_to = models.JSONField(
        help_text="WorldKG class distribution at t1: {class: count}"
    )

    # Drift metrics
    kl_divergence = models.FloatField(
        help_text="KL divergence between class distributions"
    )
    js_divergence = models.FloatField(
        help_text="Jensen-Shannon divergence (symmetric)"
    )
    depth_delta = models.FloatField(
        help_text="Change in mean ontology depth (negative = generalization)"
    )

    # Class changes
    new_classes = models.JSONField(
        help_text="Classes appearing in t1 but not t0"
    )
    lost_classes = models.JSONField(
        help_text="Classes in t0 but not t1"
    )
    dominant_shift = models.JSONField(
        help_text="Largest class proportion changes: {class: delta}"
    )

    # Semantic interpretation
    drift_magnitude = models.CharField(
        max_length=20,
        choices=[
            ('low', 'Low (<0.1)'),
            ('medium', 'Medium (0.1-0.3)'),
            ('high', 'High (0.3-0.5)'),
            ('extreme', 'Extreme (>0.5)')
        ],
        help_text="Categorical drift magnitude based on JS divergence"
    )

    processing_session = models.ForeignKey(
        'core.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='wkg_class_drifts',
        help_text="Pipeline session that computed this drift"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'worldkg_class_drift'
        unique_together = ['region', 'snapshot_from', 'snapshot_to']
        ordering = ['region', 'snapshot_from__snapshot_date']
        indexes = [
            models.Index(fields=['region', 'drift_magnitude']),
            models.Index(fields=['snapshot_from', 'snapshot_to']),
            models.Index(fields=['-js_divergence']),
        ]

    def __str__(self):
        return f"WorldKG Drift: {self.region} ({self.snapshot_from.snapshot_date} → {self.snapshot_to.snapshot_date})"


class WorldKGClassFingerprint(models.Model):
    """Monthly ontological fingerprint for a region.

    Used for DoppelCity comparisons and temporal analysis.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.CharField(max_length=255, db_index=True)
    snapshot = models.ForeignKey(
        'osmsnapshot.Snapshot',
        on_delete=models.CASCADE,
        related_name='wkg_fingerprints',
        help_text="Snapshot this fingerprint represents"
    )

    # Class distribution (normalized to percentages)
    class_distribution = models.JSONField(
        help_text="Normalized class distribution: {class: percentage}"
    )

    # Ontology statistics
    mean_depth = models.FloatField(
        help_text="Mean ontology depth across all entities"
    )
    depth_std = models.FloatField(
        help_text="Standard deviation of ontology depth"
    )
    unique_classes = models.IntegerField(
        help_text="Number of unique WorldKG classes present"
    )

    # Diversity metrics
    shannon_entropy = models.FloatField(
        help_text="Shannon entropy of class distribution (semantic diversity)"
    )
    simpson_index = models.FloatField(
        help_text="Simpson's diversity index"
    )

    # Top classes (for quick comparison)
    top_5_classes = models.JSONField(
        help_text="Top 5 most common classes with percentages"
    )

    total_entities = models.BigIntegerField(
        help_text="Total entities with WorldKG classification"
    )

    processing_session = models.ForeignKey(
        'core.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='wkg_fingerprints',
        help_text="Pipeline session that generated this fingerprint"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'worldkg_class_fingerprint'
        unique_together = ['region', 'snapshot']
        ordering = ['region', 'snapshot__snapshot_date']
        indexes = [
            models.Index(fields=['region', 'snapshot']),
            models.Index(fields=['shannon_entropy']),
            models.Index(fields=['mean_depth']),
        ]

    def __str__(self):
        return f"WorldKG Fingerprint: {self.region} @ {self.snapshot.snapshot_date}"
