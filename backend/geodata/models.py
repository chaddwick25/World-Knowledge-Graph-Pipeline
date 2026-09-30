from django.db import models
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GinIndex
from django.utils import timezone


class DataSource(models.Model):
    """A data source portal (e.g., Toronto Open Data, StatCan)."""
    ADAPTER_CHOICES = [
        ('ckan', 'CKAN'),
        ('socrata', 'Socrata'),
        ('wfs', 'WFS/WMS'),
        ('direct', 'Direct Download'),
        ('rest', 'REST API'),
    ]

    name = models.CharField(max_length=200, unique=True)
    adapter_type = models.CharField(max_length=50, choices=ADAPTER_CHOICES)
    base_url = models.URLField(max_length=1000)
    api_key = models.CharField(max_length=500, blank=True, null=True)
    country_code = models.CharField(max_length=10, blank=True)
    is_active = models.BooleanField(default=True)
    config = models.JSONField(default=dict)  # adapter-specific config
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'geodata_source'
        ordering = ['name']

    def __str__(self):
        return self.name


class GeoDataset(models.Model):
    """A dataset from a source (e.g., TTC Routes, Census Boundaries)."""
    source = models.ForeignKey(
        DataSource,
        on_delete=models.CASCADE,
        related_name='datasets',
    )
    source_dataset_id = models.CharField(max_length=200)  # CKAN pkg ID, StatCan table ID
    title = models.CharField(max_length=500)
    name = models.CharField(max_length=200)  # slug
    description = models.TextField(blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    license = models.CharField(max_length=200, blank=True)
    refresh_rate = models.CharField(max_length=100, blank=True)
    spatial_extent = gis_models.GeometryField(null=True, blank=True, srid=4326)
    temporal_start = models.DateField(null=True, blank=True)
    temporal_end = models.DateField(null=True, blank=True)
    keywords = models.JSONField(default=list)
    is_retired = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict)  # full source metadata
    last_synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'geodata_dataset'
        ordering = ['-last_synced_at']
        unique_together = [['source', 'source_dataset_id']]
        indexes = [
            models.Index(fields=['source', 'is_retired']),
            models.Index(fields=['refresh_rate']),
        ]

    def __str__(self):
        return f"{self.title} ({self.source.name})"


class GeoResource(models.Model):
    """An individual file within a dataset."""
    dataset = models.ForeignKey(
        GeoDataset,
        on_delete=models.CASCADE,
        related_name='resources',
    )
    source_resource_id = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=500)
    format = models.CharField(max_length=50, db_index=True)  # CSV, JSON, SHP, GTFS, GPKG
    url = models.URLField(max_length=1000)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    mimetype = models.CharField(max_length=100, blank=True, null=True)
    last_modified = models.DateTimeField(null=True, blank=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'geodata_resource'
        ordering = ['-last_modified']
        indexes = [
            models.Index(fields=['dataset', 'format']),
        ]

    def __str__(self):
        return f"{self.name} ({self.format})"


class DataQualitySnapshot(models.Model):
    """Time-series quality scores for datasets."""
    GRADE_CHOICES = [
        ('Bronze', 'Bronze'),
        ('Silver', 'Silver'),
        ('Gold', 'Gold'),
        ('Unknown', 'Unknown'),
    ]

    dataset = models.ForeignKey(
        GeoDataset,
        on_delete=models.CASCADE,
        related_name='quality_snapshots',
    )
    quality_score_pct = models.FloatField(null=True, blank=True)
    grade = models.CharField(max_length=20, choices=GRADE_CHOICES, default='Unknown')
    freshness = models.FloatField(null=True, blank=True)
    metadata_score = models.FloatField(null=True, blank=True)
    usability = models.FloatField(null=True, blank=True)
    completeness = models.FloatField(null=True, blank=True)
    accessibility = models.FloatField(null=True, blank=True)
    qa_recorded_at = models.DateTimeField(null=True, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'geodata_quality_snapshot'
        ordering = ['-ingested_at']
        indexes = [
            models.Index(fields=['dataset', '-qa_recorded_at']),
        ]

    def __str__(self):
        return f"{self.dataset.name} - {self.grade} ({self.quality_score_pct}%)"


class IngestionState(models.Model):
    """Track incremental ingestion state for temporal datasets."""
    INGESTION_MODE_CHOICES = [
        ('full', 'Full'),
        ('incremental', 'Incremental'),
    ]

    resource = models.OneToOneField(
        GeoResource,
        on_delete=models.CASCADE,
        related_name='ingestion_state',
        primary_key=True,
    )
    last_ingested_date = models.DateField(null=True, blank=True)
    last_ingested_datetime = models.DateTimeField(null=True, blank=True)
    last_ingested_count = models.IntegerField(default=0)
    total_records_ingested = models.BigIntegerField(default=0)
    last_ingestion_mode = models.CharField(
        max_length=20,
        choices=INGESTION_MODE_CHOICES,
        default='full',
    )
    last_ingestion_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'geodata_ingestion_state'
        ordering = ['-last_ingestion_at']

    def __str__(self):
        last = self.last_ingested_date or self.last_ingested_datetime or 'Never'
        return f"{self.resource.name} - Last: {last}"


class GeoRecord(models.Model):
    """A single record from any dataset — flexible schema via JSON."""
    dataset = models.ForeignKey(
        GeoDataset,
        on_delete=models.CASCADE,
        related_name='records',
    )
    resource = models.ForeignKey(
        GeoResource,
        on_delete=models.CASCADE,
        related_name='records',
    )
    source_id = models.CharField(max_length=200, db_index=True)  # row ID from source
    attributes = models.JSONField(default=dict)  # the actual data — flexible schema
    geom = gis_models.GeometryField(null=True, blank=True, srid=4326)  # PostGIS geometry
    ingestion_date = models.DateField(null=True, blank=True, db_index=True)  # for temporal data
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'geodata_record'
        ordering = ['-imported_at']
        indexes = [
            models.Index(fields=['resource', 'ingestion_date']),
            models.Index(fields=['source_id']),
            GinIndex(fields=['attributes'], name='geodata_record_attrs_gin'),
        ]

    def __str__(self):
        return f"{self.resource.name} - {self.source_id}"
