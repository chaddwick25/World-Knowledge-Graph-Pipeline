from django.db import models
from django.utils import timezone


class CkanDataset(models.Model):
    """Represents a CKAN package/dataset from Toronto Open Data"""
    ckan_id = models.CharField(max_length=200, unique=True, db_index=True)
    title = models.CharField(max_length=500)
    name = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    refresh_rate = models.CharField(max_length=100, blank=True)
    is_retired = models.BooleanField(default=False, db_index=True)
    owner_org = models.CharField(max_length=200, blank=True)
    metadata_created = models.DateTimeField(null=True, blank=True)
    metadata_modified = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(auto_now=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'toronto_ckan_dataset'
        ordering = ['-last_synced_at']

    def __str__(self):
        return self.ckan_id


class QualitySnapshot(models.Model):
    """Time-series quality scores for datasets (Bronze/Silver/Gold grading)"""
    GRADE_CHOICES = [
        ('Bronze', 'Bronze'),
        ('Silver', 'Silver'),
        ('Gold', 'Gold'),
        ('Unknown', 'Unknown'),
    ]

    dataset = models.ForeignKey(
        CkanDataset,
        on_delete=models.CASCADE,
        related_name='snapshots'
    )
    quality_score_pct = models.FloatField(null=True, blank=True)
    grade = models.CharField(max_length=20, choices=GRADE_CHOICES, default='Unknown')
    freshness = models.FloatField(null=True, blank=True)
    metadata_score = models.FloatField(null=True, blank=True)
    usability = models.FloatField(null=True, blank=True)
    completeness = models.FloatField(null=True, blank=True)
    accessibility = models.FloatField(null=True, blank=True)
    qa_recorded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ingested_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'toronto_quality_snapshot'
        ordering = ['-ingested_at']
        indexes = [
            models.Index(fields=['dataset', '-qa_recorded_at']),
        ]

    def __str__(self):
        return f"{self.dataset.ckan_id} - {self.grade} ({self.quality_score_pct}%)"


class CkanResource(models.Model):
    """Individual data files/resources within a dataset"""
    dataset = models.ForeignKey(
        CkanDataset,
        on_delete=models.CASCADE,
        related_name='resources'
    )
    ckan_resource_id = models.CharField(max_length=200, unique=True, db_index=True)
    name = models.CharField(max_length=500)
    format = models.CharField(max_length=50, db_index=True)
    url = models.URLField(max_length=1000)
    size = models.BigIntegerField(null=True, blank=True)
    mimetype = models.CharField(max_length=100, blank=True, null=True)
    last_modified = models.DateTimeField(null=True, blank=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'toronto_ckan_resource'
        ordering = ['-last_modified']
        indexes = [
            models.Index(fields=['dataset', 'format']),
        ]

    def __str__(self):
        return f"{self.name} ({self.format})"


class DatasetIngestionState(models.Model):
    """Track incremental ingestion state for temporal datasets"""
    resource = models.OneToOneField(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='ingestion_state',
        primary_key=True
    )
    last_ingested_date = models.DateField(null=True, blank=True, db_index=True)
    last_ingested_datetime = models.DateTimeField(null=True, blank=True, db_index=True)
    last_ingested_count = models.IntegerField(default=0)
    total_records_ingested = models.BigIntegerField(default=0)
    last_ingestion_mode = models.CharField(
        max_length=20,
        choices=[('full', 'Full'), ('incremental', 'Incremental')],
        default='full'
    )
    last_ingestion_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'toronto_ingestion_state'
        ordering = ['-last_ingestion_at']

    def __str__(self):
        return f"{self.resource.name} - Last: {self.last_ingested_date or self.last_ingested_datetime or 'Never'}"


class TrafficVolume(models.Model):
    """Actual traffic data from CSV file"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='traffic_volumes'
    )
    
    # CSV columns - will be populated based on actual schema
    intersection_id = models.CharField(max_length=100, db_index=True)
    location = models.CharField(max_length=500)
    date = models.DateField(db_index=True, null=True, blank=True)
    time_period = models.CharField(max_length=100, blank=True)
    vehicle_count = models.IntegerField(null=True, blank=True)
    pedestrian_count = models.IntegerField(null=True, blank=True)
    cyclist_count = models.IntegerField(null=True, blank=True)
    
    # Spatial fields
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    
    # Additional fields for flexible CSV parsing
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'toronto_traffic_volume'
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date', 'intersection_id']),
            models.Index(fields=['resource', 'date']),
        ]

    def __str__(self):
        return f"{self.location} - {self.date}"


class TtcSubwayDelay(models.Model):
    """TTC delay incidents from CSV"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='ttc_delays'
    )
    
    # CSV columns
    date = models.DateField(db_index=True)
    time = models.TimeField(null=True, blank=True)
    station = models.CharField(max_length=200, db_index=True)
    line = models.CharField(max_length=100, db_index=True)
    delay_minutes = models.IntegerField(null=True, blank=True)
    delay_code = models.CharField(max_length=50, blank=True)
    delay_reason = models.TextField(blank=True)
    vehicle_number = models.CharField(max_length=50, blank=True)
    
    # Additional fields
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'toronto_ttc_subway_delay'
        ordering = ['-date', '-time']
        indexes = [
            models.Index(fields=['date', 'station']),
            models.Index(fields=['line', 'date']),
        ]

    def __str__(self):
        return f"{self.station} - {self.date} ({self.delay_minutes}min)"


class CafetoLocation(models.Model):
    """CaféTO parklet locations from CSV"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='cafeto_locations'
    )
    
    # CSV columns
    business_name = models.CharField(max_length=500)
    address = models.CharField(max_length=500)
    ward = models.CharField(max_length=100, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    installation_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=100, db_index=True)
    
    # Additional fields
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'toronto_cafeto_location'
        ordering = ['business_name']
        indexes = [
            models.Index(fields=['ward', 'status']),
            models.Index(fields=['latitude', 'longitude']),
        ]

    def __str__(self):
        return f"{self.business_name} - {self.address}"


# ============================================================================
# SPATIAL INFRASTRUCTURE MODELS (Graph Foundation)
# ============================================================================

class TorontoCentreline(models.Model):
    """Road network edges - LineString geometries"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='centreline_segments'
    )
    centreline_id = models.CharField(max_length=100, db_index=True)
    linear_name_full = models.CharField(max_length=500)
    address_l = models.CharField(max_length=200, blank=True)
    address_r = models.CharField(max_length=200, blank=True)
    from_intersection_id = models.CharField(max_length=100, db_index=True)
    to_intersection_id = models.CharField(max_length=100, db_index=True)
    feature_code = models.CharField(max_length=50, db_index=True)
    geometry = models.JSONField()  # GeoJSON LineString
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_centreline'
        ordering = ['centreline_id']
        indexes = [
            models.Index(fields=['centreline_id']),
            models.Index(fields=['from_intersection_id', 'to_intersection_id']),
            models.Index(fields=['feature_code']),
        ]
    
    def __str__(self):
        return f"{self.linear_name_full} ({self.centreline_id})"


class IntersectionFile(models.Model):
    """Road network nodes - Point geometries"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='intersections'
    )
    intersection_id = models.CharField(max_length=100, unique=True, db_index=True)
    intersection_desc = models.CharField(max_length=500)
    latitude = models.FloatField()
    longitude = models.FloatField()
    elevation = models.FloatField(null=True, blank=True)
    geometry = models.JSONField()  # GeoJSON Point
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_intersection'
        ordering = ['intersection_id']
        indexes = [
            models.Index(fields=['latitude', 'longitude']),
        ]
    
    def __str__(self):
        return f"{self.intersection_desc} ({self.intersection_id})"


class CyclingNetwork(models.Model):
    """Cycling infrastructure edges"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='cycling_segments'
    )
    segment_id = models.CharField(max_length=100, db_index=True)
    street_name = models.CharField(max_length=500)
    infrastructure_type = models.CharField(max_length=100, db_index=True)
    from_street = models.CharField(max_length=200, blank=True)
    to_street = models.CharField(max_length=200, blank=True)
    length_m = models.FloatField(null=True, blank=True)
    geometry = models.JSONField()  # GeoJSON LineString
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_cycling_network'
        ordering = ['street_name']
        indexes = [
            models.Index(fields=['infrastructure_type']),
        ]
    
    def __str__(self):
        return f"{self.street_name} - {self.infrastructure_type}"


class Neighbourhood(models.Model):
    """Neighbourhood boundaries - Polygon geometries"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='neighbourhoods'
    )
    neighbourhood_id = models.CharField(max_length=100, unique=True, db_index=True)
    neighbourhood_name = models.CharField(max_length=200, db_index=True)
    area_sqkm = models.FloatField(null=True, blank=True)
    geometry = models.JSONField()  # GeoJSON Polygon/MultiPolygon
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_neighbourhood'
        ordering = ['neighbourhood_name']
    
    def __str__(self):
        return self.neighbourhood_name


class ZoningByLaw(models.Model):
    """Zoning classifications - static node labels"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='zoning_areas'
    )
    zone_id = models.CharField(max_length=100, db_index=True)
    zone_category = models.CharField(max_length=100, db_index=True)
    zone_label = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    geometry = models.JSONField()  # GeoJSON Polygon/MultiPolygon
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_zoning'
        ordering = ['zone_category', 'zone_label']
        indexes = [
            models.Index(fields=['zone_category']),
        ]
    
    def __str__(self):
        return f"{self.zone_label} ({self.zone_category})"


class BusinessImprovementArea(models.Model):
    """Business Improvement Areas - semantic overlay"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='bia_areas'
    )
    bia_id = models.CharField(max_length=100, unique=True, db_index=True)
    bia_name = models.CharField(max_length=200, db_index=True)
    area_sqkm = models.FloatField(null=True, blank=True)
    geometry = models.JSONField()  # GeoJSON Polygon/MultiPolygon
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_bia'
        ordering = ['bia_name']
    
    def __str__(self):
        return self.bia_name


# ============================================================================
# TEMPORAL FLOW MODELS (Time-Series Data)
# ============================================================================

class BicycleCounter(models.Model):
    """Permanent bicycle counter readings - flow data"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='bicycle_counts'
    )
    location_id = models.CharField(max_length=100, db_index=True)
    location_name = models.CharField(max_length=500)
    count_date = models.DateField(db_index=True)
    count_time = models.TimeField(null=True, blank=True)
    count_value = models.IntegerField()
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_bicycle_counter'
        ordering = ['-count_date', '-count_time']
        indexes = [
            models.Index(fields=['count_date', 'location_id']),
            models.Index(fields=['latitude', 'longitude']),
        ]
    
    def __str__(self):
        return f"{self.location_name} - {self.count_date}"


class TtcRoute(models.Model):
    """TTC routes and schedules - transit network"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='ttc_routes'
    )
    route_id = models.CharField(max_length=100, db_index=True)
    route_name = models.CharField(max_length=200)
    route_type = models.CharField(max_length=50, db_index=True)
    geometry = models.JSONField(null=True, blank=True)  # GeoJSON LineString
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_ttc_route'
        ordering = ['route_name']
    
    def __str__(self):
        return f"{self.route_name} ({self.route_type})"


class RainGauge(models.Model):
    """Rain gauge precipitation readings - environmental covariate"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='rain_readings'
    )
    station_id = models.CharField(max_length=100, db_index=True)
    station_name = models.CharField(max_length=200)
    measurement_date = models.DateField(db_index=True)
    measurement_time = models.TimeField(null=True, blank=True)
    precipitation_mm = models.FloatField()
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_rain_gauge'
        ordering = ['-measurement_date', '-measurement_time']
        indexes = [
            models.Index(fields=['measurement_date', 'station_id']),
        ]
    
    def __str__(self):
        return f"{self.station_name} - {self.measurement_date}"


class ZoningReview(models.Model):
    """Preliminary zoning reviews - development pressure (daily)"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='zoning_reviews'
    )
    application_id = models.CharField(max_length=100, unique=True, db_index=True)
    application_date = models.DateField(db_index=True)
    address = models.CharField(max_length=500)
    proposal_description = models.TextField()
    status = models.CharField(max_length=100, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_zoning_review'
        ordering = ['-application_date']
        indexes = [
            models.Index(fields=['application_date', 'status']),
        ]
    
    def __str__(self):
        return f"{self.application_id} - {self.address}"


class NeighbourhoodProfile(models.Model):
    """Census neighbourhood profiles - temporal snapshots"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='neighbourhood_profiles'
    )
    neighbourhood_id = models.CharField(max_length=100, db_index=True)
    census_year = models.IntegerField(db_index=True)
    population = models.IntegerField(null=True, blank=True)
    households = models.IntegerField(null=True, blank=True)
    median_income = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_neighbourhood_profile'
        ordering = ['neighbourhood_id', '-census_year']
        indexes = [
            models.Index(fields=['neighbourhood_id', 'census_year']),
        ]
        unique_together = [['neighbourhood_id', 'census_year']]
    
    def __str__(self):
        return f"{self.neighbourhood_id} - {self.census_year}"


# ============================================================================
# ADDITIONAL DATASETS
# ============================================================================

class ForestLandCover(models.Model):
    """Forest and land cover data"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='forest_land_cover'
    )
    feature_id = models.CharField(max_length=100, db_index=True)
    land_cover_type = models.CharField(max_length=100, db_index=True)
    area_sqm = models.FloatField(null=True, blank=True)
    geometry = models.JSONField()  # GeoJSON Polygon/MultiPolygon
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_forest_land_cover'
        ordering = ['land_cover_type']
    
    def __str__(self):
        return f"{self.land_cover_type} - {self.feature_id}"


class CommitteeAdjustmentApplication(models.Model):
    """Committee of adjustment applications"""
    resource = models.ForeignKey(
        CkanResource,
        on_delete=models.CASCADE,
        related_name='committee_applications'
    )
    application_number = models.CharField(max_length=100, unique=True, db_index=True)
    application_date = models.DateField(db_index=True)
    address = models.CharField(max_length=500)
    application_type = models.CharField(max_length=100, db_index=True)
    status = models.CharField(max_length=100, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'toronto_committee_adjustment'
        ordering = ['-application_date']
        indexes = [
            models.Index(fields=['application_date', 'status']),
        ]
    
    def __str__(self):
        return f"{self.application_number} - {self.address}"
