from django.db import models
import uuid


# TODO: refactor this
class TemporalSnapshot(models.Model):
    """
    Temporal snapshot - exists solely to generate AssetBundles.
    Always has a 1:1 relationship with AssetBundle.
    """
    
    class SnapshotInterval(models.TextChoices):
        WEEKLY = 'WEEKLY', 'Weekly (every 7 days)'
        BIWEEKLY = 'BIWEEKLY', 'Biweekly (every 14 days)'
        MONTHLY = 'MONTHLY', 'Monthly (last day of month)'
        CUSTOM = 'CUSTOM', 'Custom date'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.CharField(max_length=255, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    
    pbf_file = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.CASCADE,
        related_name='temporal_snapshots',
        help_text="Source temporal extract (monthly or yearly)"
    )
    
    snapshot_interval = models.CharField(
        max_length=20,
        choices=SnapshotInterval.choices,
        default='MONTHLY',
        help_text="Interval type for this snapshot"
    )
    
    file_path = models.CharField(
        max_length=1024,
        unique=True,
        null=True,
        blank=True,
        help_text="Optional: Path to snapshot PBF if generated"
    )
    
    filter_config = models.JSONField(
        null=True,
        blank=True,
        help_text="Filter configuration (target_tags, etc.) for filtered snapshots"
    )
    filter_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="MD5 hash of filter_config for uniqueness constraint"
    )
    
    node_count = models.BigIntegerField(default=0)
    way_count = models.BigIntegerField(default=0)
    relation_count = models.BigIntegerField(default=0)
    
    fasttext_model_path = models.CharField(max_length=512, null=True, blank=True)
    nle_model_path = models.CharField(max_length=512, null=True, blank=True)
    
    processing_session = models.ForeignKey(
        'orchestration.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='temporal_snapshots',
        help_text="Pipeline session that created this snapshot"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'temporal_snapshots'
        unique_together = ['region', 'timestamp', 'snapshot_interval', 'filter_hash']
        ordering = ['region', 'timestamp']
        indexes = [
            models.Index(fields=['region', 'timestamp']),
            models.Index(fields=['pbf_file', 'snapshot_interval']),
            models.Index(fields=['snapshot_interval', 'created_at'])
        ]
    
    def __str__(self):
        return f"Snapshot: {self.region} @ {self.timestamp} ({self.snapshot_interval})"


class AssetBundle(models.Model):
    """
    PRIMARY DATA ARTIFACT - Raw OSM data extracted from filtered snapshots.
    This is the source of truth for all downstream ML tasks.
    Contains extracted OSM data: nodes, ways, relations, edges, tags.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    
    temporal_snapshot = models.ForeignKey(
        TemporalSnapshot,
        on_delete=models.CASCADE,
        related_name='asset_bundles',
        help_text="Associated temporal snapshot"
    )
    
    bundle_path = models.CharField(
        max_length=1024,
        help_text="Directory path containing asset files"
    )
    
    # Asset statistics
    node_count = models.IntegerField(default=0)
    way_count = models.IntegerField(default=0)
    relation_count = models.IntegerField(default=0)
    edge_count = models.IntegerField(default=0)
    tag_count = models.IntegerField(default=0)
    
    total_size_bytes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Total size of all asset files in bytes"
    )
    
    generation_time_seconds = models.FloatField(
        null=True,
        blank=True,
        help_text="Time taken to generate this asset bundle"
    )
    
    # Lineage tracking
    source_monthly_extract = models.ForeignKey(
        'extraction.PbfFile',
        on_delete=models.PROTECT,
        related_name='derived_asset_bundles',
        null=True,
        blank=True,
        help_text="Original monthly extract this was derived from"
    )
    
    processing_session = models.ForeignKey(
        'orchestration.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='asset_bundles',
        help_text="Pipeline session that generated this asset bundle"
    )
    
    # Tag key for organization (KEY only, not key=value)
    tag_key = models.CharField(
        max_length=100,
        db_index=True,
        null=True,
        blank=True,
        help_text="OSM tag key (building, amenity, highway, etc.)"
    )
    
    # Optional name for the asset bundle (for tag discovery workflows)
    name = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        db_index=True,
        help_text="Optional descriptive name for this asset bundle"
    )
    
    # Quality metrics
    data_quality_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Quality score based on completeness, consistency"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'asset_bundles'
        ordering = ['-created_at']
        unique_together = [['temporal_snapshot', 'tag_key']]
        indexes = [
            models.Index(fields=['temporal_snapshot', 'created_at']),
            models.Index(fields=['created_at']),
            models.Index(fields=['tag_key', 'created_at']),
            models.Index(fields=['source_monthly_extract', 'tag_key']),
        ]
    
    def __str__(self):
        tag_info = f" [{self.tag_key}]" if self.tag_key else ""
        return f"AssetBundle for {self.temporal_snapshot.region} at {self.temporal_snapshot.timestamp}{tag_info}"




class PbfTagDistribution(models.Model):
    """Store individual tag distributions for PBF files"""
    
    pbf_file = models.ForeignKey('extraction.PbfFile', on_delete=models.CASCADE, related_name='tag_distributions')
    processing_session = models.ForeignKey(
        'orchestration.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tag_distributions',
        help_text="Pipeline session that produced this tag distribution"
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
    """
    Tracks ontological class distribution drift over time for a region.
    Enables semantic change detection beyond raw embedding drift.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.CharField(max_length=255, db_index=True)
    bbox = models.JSONField(
        help_text="Bounding box as [min_lon, min_lat, max_lon, max_lat]"
    )
    
    snapshot_from = models.ForeignKey(
        TemporalSnapshot,
        on_delete=models.CASCADE,
        related_name='wkg_drift_from',
        help_text="Earlier temporal snapshot"
    )
    snapshot_to = models.ForeignKey(
        TemporalSnapshot,
        on_delete=models.CASCADE,
        related_name='wkg_drift_to',
        help_text="Later temporal snapshot"
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
        'orchestration.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='wkg_class_drifts',
        help_text="Pipeline session that computed this drift"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'worldkg_class_drift'
        unique_together = ['region', 'snapshot_from', 'snapshot_to']
        ordering = ['region', 'snapshot_from__timestamp']
        indexes = [
            models.Index(fields=['region', 'drift_magnitude']),
            models.Index(fields=['snapshot_from', 'snapshot_to']),
            models.Index(fields=['-js_divergence']),
        ]
    
    def __str__(self):
        return f"WorldKG Drift: {self.region} ({self.snapshot_from.timestamp} → {self.snapshot_to.timestamp})"


class WorldKGClassFingerprint(models.Model):
    """
    Monthly ontological fingerprint for a region.
    Used for DoppelCity comparisons and temporal analysis.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.CharField(max_length=255, db_index=True)
    snapshot = models.ForeignKey(
        TemporalSnapshot,
        on_delete=models.CASCADE,
        related_name='wkg_fingerprints',
        help_text="Temporal snapshot this fingerprint represents"
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
        'orchestration.ProcessingSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='wkg_fingerprints',
        help_text="Pipeline session that generated this fingerprint"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'worldkg_class_fingerprint'
        unique_together = ['region', 'snapshot']
        ordering = ['region', 'snapshot__timestamp']
        indexes = [
            models.Index(fields=['region', 'snapshot']),
            models.Index(fields=['shannon_entropy']),
            models.Index(fields=['mean_depth']),
        ]
    
    def __str__(self):
        return f"WorldKG Fingerprint: {self.region} @ {self.snapshot.timestamp}"
