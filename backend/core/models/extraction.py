from django.db import models
import uuid
import os
import logging
from pathlib import Path
from datetime import datetime
from django.utils import timezone
from django.contrib.postgres.fields import ArrayField
logger = logging.getLogger(__name__)


class PbfFile(models.Model):
    class PbfStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        DOWNLOADING = 'DOWNLOADING', 'Downloading'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
    
    class PbfType(models.TextChoices):
        PLANET = 'PLANET', 'Planet History File'
        CONTINENT = 'CONTINENT', 'Continent Extract'
        REGION = 'REGION', 'Region Extract'
        LATEST = 'LATEST', 'Latest Snapshot'
        HISTORICAL = 'HISTORICAL', 'Full History'
    
    class ExtractionLevel(models.TextChoices):
        PLANET = 'PLANET', 'Planet'
        CONTINENT = 'CONTINENT', 'Continent'
        REGION = 'REGION', 'Region Extract'
        REGION_YEARLY = 'REGION_YEARLY', 'Region Yearly Extract'
        REGION_MONTHLY = 'REGION_MONTHLY', 'Region Monthly Extract' 
        REGION_DAILY = 'REGION_DAILY', 'Region Daily Snapshot'
        REGION_CUSTOM = 'REGION_CUSTOM', 'Region Custom Extract'
        SNAPSHOT = 'SNAPSHOT', 'Temporal Snapshot'
    
    class TemporalMetadataSource(models.TextChoices):
        AUTO_DETECTED = 'AUTO_DETECTED', 'Auto-detected from file'
        INHERITED = 'INHERITED', 'Inherited from parent'
        MANUAL = 'MANUAL', 'Manually specified'
        COMPUTED = 'COMPUTED', 'Computed from settings'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    pbf_file_type = models.CharField(
        max_length=20,
        choices=PbfType.choices,
        default=PbfType.REGION
    )
    
    source_url = models.URLField(max_length=1024, null=True, blank=True)
    path = models.CharField(max_length=500, unique=True, null=True, blank=True)
    status = models.CharField(max_length=20, choices=PbfStatus.choices, default=PbfStatus.PENDING)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    has_history = models.BooleanField(default=False)
    format_version = models.CharField(max_length=50, null=True, blank=True)
    file_type = models.CharField(max_length=50, null=True, blank=True)
    compression = models.CharField(max_length=50, null=True, blank=True)
    generator = models.CharField(max_length=200, null=True, blank=True)
    raw_info = models.JSONField(default=dict, blank=True)
    min_timestamp = models.DateTimeField(blank=True, null=True, help_text="The earliest timestamp found in the PBF file")
    max_timestamp = models.DateTimeField(blank=True, null=True, help_text="The latest timestamp found in the PBF file")
    registered_at = models.DateTimeField(auto_now_add=True)
    
    parent_pbf = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_extracts',
        help_text="Parent PBF file this was extracted from"
    )
    extraction_level = models.CharField(
        max_length=20,
        choices=ExtractionLevel.choices,
        null=True,
        blank=True,
        help_text="Granularity level of this extraction"
    )
    temporal_metadata_source = models.CharField(
        max_length=20,
        choices=TemporalMetadataSource.choices,
        null=True,
        blank=True,
        help_text="How temporal metadata was determined"
    )
    
    yearly_extracts_generated = models.BooleanField(
        default=False,
        help_text="Whether yearly extracts have been generated for this region"
    )
    yearly_extracts_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when all yearly extracts were completed"
    )
    yearly_extracts_count = models.IntegerField(
        default=0,
        help_text="Number of yearly extracts created"
    )
    yearly_extracts_year_range = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Year range of extracts (e.g., '2006-2024')"
    )
    
    monthly_extracts_generated = models.BooleanField(
        default=False,
        help_text="Whether monthly extracts have been generated for this region"
    )
    monthly_extracts_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when all monthly extracts were completed"
    )
    monthly_extracts_count = models.IntegerField(
        default=0,
        help_text="Number of monthly extracts created"
    )
    monthly_extracts_year_range = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Year range of monthly extracts (e.g., '2006-2024')"
    )
    
    class Meta:
        db_table = 'pbf_files'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['pbf_file_type']),
            models.Index(fields=['parent_pbf']),
            models.Index(fields=['extraction_level']),
            models.Index(fields=['registered_at']),
        ]
    
    def get_download_directory(self):
        """Returns the appropriate download directory based on file type"""
        from django.conf import settings
        base_dir = settings.DOWNLOADS_DIR
        if self.pbf_file_type == self.PbfType.HISTORICAL:
            return Path(base_dir) / 'historical'
        return Path(base_dir) / 'latest'

    def get_temporal_range(self):
        """Get temporal range for snapshot generation"""
        from django.conf import settings
        from django.utils import timezone
        
        end_time = self.max_timestamp
        if not end_time:
            end_time = getattr(settings, 'DEFAULT_TEMPORAL_RANGE_END', timezone.now())
        
        return {
            'start': self.min_timestamp,
            'end': end_time,
            'source': self.temporal_metadata_source or 'COMPUTED'
        }
    
    def get_effective_max_timestamp(self):
        """Get max timestamp with fallback logic"""
        from django.conf import settings
        from django.utils import timezone
        
        if self.max_timestamp:
            return self.max_timestamp
        
        try:
            metrics = self.osmiumdatasetmetrics_set.first()
            if metrics and metrics.temporal_coverage_end:
                return metrics.temporal_coverage_end
        except Exception as e:
            logger.debug(f"Failed to get temporal coverage from metrics: {e}")
        
        if hasattr(settings, 'DEFAULT_TEMPORAL_RANGE_END'):
            return settings.DEFAULT_TEMPORAL_RANGE_END
        
        return timezone.now()
    
    def get_extraction_hierarchy(self):
        """Get the full extraction hierarchy path"""
        hierarchy = []
        current = self
        while current:
            hierarchy.append({
                'id': str(current.id),
                'path': current.path,
                'extraction_level': current.extraction_level,
                'temporal_range': {
                    'start': current.min_timestamp.isoformat() if current.min_timestamp else None,
                    'end': current.max_timestamp.isoformat() if current.max_timestamp else None
                }
            })
            current = current.parent_pbf
        return list(reversed(hierarchy))
    
    def get_latest_monthly_extract(self):
        """
        Get the most recent monthly extract from this PBF's hierarchy.
        
        Traverses: self → yearly extracts → monthly extracts
        Returns the monthly extract with the latest max_timestamp.
        
        Returns:
            PbfFile or None: The most recent monthly extract, or None if no monthly extracts exist
        """
        # Get all yearly extracts that are children of this PBF
        yearly_extracts = PbfFile.objects.filter(
            parent_pbf=self,
            extraction_level=self.ExtractionLevel.REGION_YEARLY,
            status=self.PbfStatus.COMPLETED
        )
        
        # Collect all monthly extracts from all yearly extracts
        monthly_extracts = []
        for yearly in yearly_extracts:
            monthly_children = PbfFile.objects.filter(
                parent_pbf=yearly,
                extraction_level=self.ExtractionLevel.REGION_MONTHLY,
                status=self.PbfStatus.COMPLETED
            )
            monthly_extracts.extend(monthly_children)
        
        if not monthly_extracts:
            return None
        
        # Find the monthly extract with the latest max_timestamp
        latest = max(monthly_extracts, key=lambda x: x.max_timestamp if x.max_timestamp else timezone.datetime.min.replace(tzinfo=timezone.utc))
        
        return latest if latest.max_timestamp else None
    
    def __str__(self):
        region_name = Path(self.path).stem if self.path else 'Unknown'
        return f"{region_name} - {self.get_pbf_file_type_display()} ({self.get_status_display()})"


class RegionHierarchy(models.Model):
    """Represents the hierarchical structure of .poly files."""
    class RegionType(models.TextChoices):
        PLANET = 'PLANET', 'Planet'
        CONTINENT = 'CONTINENT', 'Continent'
        COUNTRY = 'COUNTRY', 'Country'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="The name of the region, e.g., 'africa', 'tanzania'")
    
    region_type = models.CharField(
        max_length=20,
        choices=RegionType.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="Type of region (PLANET, CONTINENT, COUNTRY)"
    )
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children'
    )
    
    poly_file_path = models.CharField(max_length=1024, unique=True, null=True, blank=True)
    
    corresponding_pbf = models.OneToOneField(
        PbfFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The generated PBF file corresponding to this region hierarchy node."
    )
    
    polygon_file = models.OneToOneField(
        'PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='region_hierarchy',
        help_text="Linked PolygonFile record for this region"
    )

    class Meta:
        db_table = 'region_hierarchy'
        unique_together = ('parent', 'name')
        ordering = ['name']

    def __str__(self):
        return self.name


class OsmBoundary(models.Model):
    """
    OSM administrative boundaries for cartographic display.
    Separate from PolygonFile (extraction boundaries).
    """
    osm_id = models.BigIntegerField(unique=True, db_index=True)
    osm_type = models.CharField(max_length=10)  # 'relation', 'way'
    admin_level = models.IntegerField(db_index=True)  # 2=country, 4=state, etc.
    
    name = models.CharField(max_length=255, db_index=True)
    name_en = models.CharField(max_length=255, null=True, blank=True)  # English name
    iso_code = models.CharField(max_length=10, null=True, blank=True, db_index=True)  # ISO3166-1
    
    # Cartographic geometry (simplified for display)
    geometry = models.JSONField(help_text="GeoJSON geometry")
    bbox = models.JSONField(null=True, blank=True, help_text="Bounding box [minLon, minLat, maxLon, maxLat]")
    
    # Link to extraction polygon
    polygon_file = models.ForeignKey(
        'PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='osm_boundary'
    )
    
    # Metadata
    simplification_tolerance = models.FloatField(default=0.01)
    
    # Temporal Validity (Versioning)
    valid_from = models.DateTimeField(
        null=True, blank=True, 
        default=datetime(2004, 1, 1, tzinfo=timezone.utc),
        help_text="Earliest timestamp when this boundary remains valid"
    )
    valid_to = models.DateTimeField(
        null=True, blank=True,
        help_text="Latest timestamp when this boundary remains valid (Null = FOREVER)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'osm_boundaries'
        indexes = [
            models.Index(fields=['admin_level', 'name']),
            models.Index(fields=['iso_code']),
        ]
        verbose_name = 'OSM Boundary'
        verbose_name_plural = 'OSM Boundaries'
    
    def __str__(self):
        return f"{self.name} (admin_level={self.admin_level})"


class PbfExtract(models.Model):
    """Tracks the metrics and parameters for an osmium PBF extract process"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_pbf = models.ForeignKey(PbfFile, on_delete=models.CASCADE, related_name='extractions')
    poly_file_path = models.CharField(max_length=500)
    output_pbf_path = models.CharField(max_length=500, unique=True)
    
    source_file_size_bytes = models.BigIntegerField()
    output_file_size_bytes = models.BigIntegerField(null=True, blank=True)
    
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    
    osmium_extract_params = models.JSONField(default=dict)
    osmium_time_filter_params = models.JSONField(default=dict, blank=True)
    
    cpu_core_id = models.IntegerField(null=True, blank=True)
    
    task = models.ForeignKey('core.Task', on_delete=models.SET_NULL, null=True, blank=True, related_name='pbf_extracts')
    
    polygon_file = models.ForeignKey(
        'PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pbf_extracts',
        help_text="Polygon file used for this extraction"
    )
    
    class Meta:
        db_table = 'pbf_extracts'
        ordering = ['-start_time']

    def __str__(self):
        region_name = Path(self.source_pbf.path).stem if self.source_pbf and self.source_pbf.path else 'Unknown Source'
        return f"Extract from {region_name} to {Path(self.output_pbf_path).name}"


class PolygonFile(models.Model):
    """Represents a .poly file used for geographic extractions."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="The name of the polygon, e.g., 'africa', 'germany', 'berlin'")
    file_path = models.CharField(max_length=1024, unique=True, help_text="The absolute path to the .poly file.")
    region_name = models.CharField(max_length=255, db_index=True, help_text="The corresponding region name, if known.")
    is_active = models.BooleanField(default=True)
    
    # Temporal Validity (Versioning)
    valid_from = models.DateTimeField(
        null=True, blank=True,
        default=datetime(2004, 1, 1, tzinfo=timezone.utc),
        help_text="Earliest timestamp when this polygon remains valid"
    )
    valid_to = models.DateTimeField(
        null=True, blank=True,
        help_text="Latest timestamp when this polygon remains valid (Null = FOREVER)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    geojson = models.JSONField(
        null=True,
        blank=True,
        help_text="Cached GeoJSON representation of polygon for map display"
    )
    geojson_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        auto_now=True,
        help_text="When GeoJSON was last generated"
    )

    class Meta:
        db_table = 'polygon_files'
        ordering = ['name']
        indexes = [
            models.Index(fields=['region_name']),
        ]

    def __str__(self):
        return self.name


class PbfCollection(models.Model):
    """Group related PBF files for batch processing"""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    pbf_files = models.ManyToManyField(PbfFile, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Collection: {self.name} ({self.pbf_files.count()} files)"

class RegionalExtractionState(models.Model):
    """
    Tracks the initialization and extraction state for continents and countries.
    This acts as the source of truth for the Recipe Builder initialization table.
    """

    class StateStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        EXTRACTING = 'EXTRACTING', 'Extracting'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        SKIPPED = 'SKIPPED', 'Skipped'

    class RegionType(models.TextChoices):
        CONTINENT = 'CONTINENT', 'Continent'
        COUNTRY = 'COUNTRY', 'Country'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region_name = models.CharField(max_length=255, db_index=True)
    region_type = models.CharField(max_length=20, choices=RegionType.choices)
    # Track which planet file was the source for this state
    source_planet_path = models.CharField(max_length=1024, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=StateStatus.choices,
        default=StateStatus.PENDING,
        db_index=True,
    )

    # Ground truth reference
    poly_file = models.ForeignKey(
        'PolygonFile',
        on_delete=models.CASCADE,
        related_name='initialization_states',
        help_text="The .poly file that defines this region's boundary",
    )

    # Links to other relevant records
    osm_boundary = models.ForeignKey(
        'OsmBoundary',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='initialization_states',
    )

    pbf_file = models.ForeignKey(
        'PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='initialization_states',
    )

    # Metrics and metadata
    metrics = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extraction metrics (nodes, ways, relations, duration, etc.)",
    )

    error_message = models.TextField(null=True, blank=True)
    last_processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regional_extraction_state'
        unique_together = ('source_planet_path', 'poly_file')
        verbose_name = 'Regional Extraction State'
        verbose_name_plural = 'Regional Extraction States'
        ordering = ['region_type', 'region_name']

    def __str__(self):
        return f"{self.region_name} ({self.region_type}) - {self.status}"


class ProjectionWeightAsset(models.Model):
    """Stores learned projection weights per country/region for tri-space scoring.

    Follows the same asset pattern as PbfFile/PolygonFile: the database is the
    source of truth, and asset_path points to an optional on-disk JSON file
    (e.g., data/projection_weights/{country_code}/weights.json).
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    country_code = models.CharField(
        max_length=10,
        db_index=True,
        help_text="ISO country code when available (e.g., 'MC', 'US')"
    )

    region_name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Human-readable region name (e.g., 'Monaco')"
    )

    asset_path = models.CharField(
        max_length=1024,
        unique=True,
        help_text="Filesystem path to the JSON file storing learned weights."
    )

    # Learned weights for tri-space style scoring
    w_geo = models.FloatField(default=1.0)
    w_name = models.FloatField(default=1.0)
    w_class = models.FloatField(default=1.0)

    validation_accuracy = models.FloatField(default=0.0)

    sample_count = models.IntegerField(default=0)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    corresponding_pbf = models.ForeignKey(
        PbfFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Optional link to the regional PBF this asset was derived from.",
        related_name='projection_weight_assets',
    )

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'projection_weight_assets'
        indexes = [
            models.Index(fields=['country_code']),
            models.Index(fields=['region_name']),
            models.Index(fields=['status']),
        ]
        unique_together = (
            ('country_code', 'region_name'),
        )

    def __str__(self):
        return f"ProjectionWeights({self.country_code}, {self.region_name})"


class PlanetaryMetrics(models.Model):
    """
    Natural primitives extracted from planetary file during initialization.
    These metrics become the foundation for OSMWikiDataHierarchy policy decisions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Source identification
    pbf_file = models.OneToOneField(
        PbfFile,
        on_delete=models.CASCADE,
        related_name='planetary_metrics'
    )
    osm_wikidata_hierarchy = models.OneToOneField(
        'core.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='planetary_metrics'
    )
    
    # Core metrics (natural primitives)
    node_count = models.BigIntegerField()
    way_count = models.BigIntegerField()
    relation_count = models.BigIntegerField()
    
    # Geographic primitives
    bbox_min_lat = models.FloatField()
    bbox_max_lat = models.FloatField()
    bbox_min_lon = models.FloatField()
    bbox_max_lon = models.FloatField()
    area_km2 = models.FloatField()
    
    # Tag distribution primitives
    unique_tag_keys = models.IntegerField()
    unique_tag_values = models.IntegerField()
    top_tag_keys = models.JSONField(default=list)
    
    # Quality primitives
    orphan_nodes_ratio = models.FloatField()
    incomplete_ways_ratio = models.FloatField()
    
    # Processing primitives (for policy decisions)
    estimated_processing_time_seconds = models.IntegerField()
    recommended_batch_size = models.IntegerField()
    recommended_worker_count = models.IntegerField()
    
    generated_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'planetary_metrics'
        indexes = [
            models.Index(fields=['pbf_file']),
            models.Index(fields=['osm_wikidata_hierarchy']),
        ]
