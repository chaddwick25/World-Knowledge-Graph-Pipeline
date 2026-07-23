from django.contrib import admin
from .models import (
    CkanDataset, QualitySnapshot, CkanResource,
    TrafficVolume, TtcSubwayDelay, CafetoLocation,
    # Spatial Infrastructure
    TorontoCentreline, IntersectionFile, CyclingNetwork,
    Neighbourhood, ZoningByLaw, BusinessImprovementArea,
    # Temporal Flows
    BicycleCounter, TtcRoute, RainGauge, ZoningReview,
    NeighbourhoodProfile,
    # Additional
    ForestLandCover, CommitteeAdjustmentApplication
)


class QualitySnapshotInline(admin.TabularInline):
    model = QualitySnapshot
    extra = 0
    readonly_fields = ('ingested_at',)
    fields = ('grade', 'quality_score_pct', 'freshness', 'qa_recorded_at', 'ingested_at')


class CkanResourceInline(admin.TabularInline):
    model = CkanResource
    extra = 0
    readonly_fields = ('created_at', 'last_downloaded_at')
    fields = ('name', 'format', 'size', 'last_modified', 'last_downloaded_at')


@admin.register(CkanDataset)
class CkanDatasetAdmin(admin.ModelAdmin):
    list_display = ('ckan_id', 'title', 'refresh_rate', 'is_retired', 'last_synced_at')
    list_filter = ('is_retired', 'refresh_rate')
    search_fields = ('ckan_id', 'title', 'name')
    readonly_fields = ('last_synced_at', 'created_at', 'updated_at')
    inlines = [CkanResourceInline, QualitySnapshotInline]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('ckan_id', 'title', 'name', 'notes')
        }),
        ('Metadata', {
            'fields': ('refresh_rate', 'is_retired', 'owner_org')
        }),
        ('Timestamps', {
            'fields': ('metadata_created', 'metadata_modified', 'last_synced_at', 'created_at', 'updated_at')
        }),
    )


@admin.register(QualitySnapshot)
class QualitySnapshotAdmin(admin.ModelAdmin):
    list_display = ('dataset', 'grade', 'quality_score_pct', 'freshness', 'qa_recorded_at', 'ingested_at')
    list_filter = ('grade', 'qa_recorded_at')
    search_fields = ('dataset__ckan_id', 'dataset__title')
    readonly_fields = ('ingested_at',)
    date_hierarchy = 'qa_recorded_at'


@admin.register(CkanResource)
class CkanResourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'dataset', 'format', 'size', 'last_modified', 'last_downloaded_at')
    list_filter = ('format', 'last_modified')
    search_fields = ('name', 'ckan_resource_id', 'dataset__title')
    readonly_fields = ('created_at', 'last_downloaded_at')


@admin.register(TrafficVolume)
class TrafficVolumeAdmin(admin.ModelAdmin):
    list_display = ('location', 'intersection_id', 'date', 'vehicle_count', 'pedestrian_count', 'cyclist_count')
    list_filter = ('date', 'resource')
    search_fields = ('location', 'intersection_id')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'date'


@admin.register(TtcSubwayDelay)
class TtcSubwayDelayAdmin(admin.ModelAdmin):
    list_display = ('station', 'line', 'date', 'time', 'delay_minutes', 'delay_code')
    list_filter = ('line', 'station', 'date')
    search_fields = ('station', 'delay_reason', 'delay_code')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'date'


@admin.register(CafetoLocation)
class CafetoLocationAdmin(admin.ModelAdmin):
    list_display = ('business_name', 'address', 'ward', 'status', 'latitude', 'longitude')
    list_filter = ('ward', 'status')
    search_fields = ('business_name', 'address')
    readonly_fields = ('imported_at',)
    
    fieldsets = (
        ('Business Information', {
            'fields': ('business_name', 'address', 'ward', 'status')
        }),
        ('Location', {
            'fields': ('latitude', 'longitude')
        }),
        ('Metadata', {
            'fields': ('installation_date', 'resource', 'imported_at')
        }),
    )


# ============================================================================
# SPATIAL INFRASTRUCTURE ADMIN
# ============================================================================

@admin.register(TorontoCentreline)
class TorontoCentrelineAdmin(admin.ModelAdmin):
    list_display = ('centreline_id', 'linear_name_full', 'feature_code', 'from_intersection_id', 'to_intersection_id')
    list_filter = ('feature_code',)
    search_fields = ('centreline_id', 'linear_name_full', 'address_l', 'address_r')
    readonly_fields = ('imported_at',)


@admin.register(IntersectionFile)
class IntersectionFileAdmin(admin.ModelAdmin):
    list_display = ('intersection_id', 'intersection_desc', 'latitude', 'longitude', 'elevation')
    search_fields = ('intersection_id', 'intersection_desc')
    readonly_fields = ('imported_at',)


@admin.register(CyclingNetwork)
class CyclingNetworkAdmin(admin.ModelAdmin):
    list_display = ('segment_id', 'street_name', 'infrastructure_type', 'length_m')
    list_filter = ('infrastructure_type',)
    search_fields = ('street_name', 'from_street', 'to_street')
    readonly_fields = ('imported_at',)


@admin.register(Neighbourhood)
class NeighbourhoodAdmin(admin.ModelAdmin):
    list_display = ('neighbourhood_id', 'neighbourhood_name', 'area_sqkm')
    search_fields = ('neighbourhood_id', 'neighbourhood_name')
    readonly_fields = ('imported_at',)


@admin.register(ZoningByLaw)
class ZoningByLawAdmin(admin.ModelAdmin):
    list_display = ('zone_id', 'zone_category', 'zone_label')
    list_filter = ('zone_category',)
    search_fields = ('zone_id', 'zone_label', 'description')
    readonly_fields = ('imported_at',)


@admin.register(BusinessImprovementArea)
class BusinessImprovementAreaAdmin(admin.ModelAdmin):
    list_display = ('bia_id', 'bia_name', 'area_sqkm')
    search_fields = ('bia_id', 'bia_name')
    readonly_fields = ('imported_at',)


# ============================================================================
# TEMPORAL FLOW ADMIN
# ============================================================================

@admin.register(BicycleCounter)
class BicycleCounterAdmin(admin.ModelAdmin):
    list_display = ('location_name', 'location_id', 'count_date', 'count_time', 'count_value')
    list_filter = ('count_date', 'location_id')
    search_fields = ('location_name', 'location_id')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'count_date'


@admin.register(TtcRoute)
class TtcRouteAdmin(admin.ModelAdmin):
    list_display = ('route_id', 'route_name', 'route_type')
    list_filter = ('route_type',)
    search_fields = ('route_id', 'route_name')
    readonly_fields = ('imported_at',)


@admin.register(RainGauge)
class RainGaugeAdmin(admin.ModelAdmin):
    list_display = ('station_name', 'station_id', 'measurement_date', 'measurement_time', 'precipitation_mm')
    list_filter = ('measurement_date', 'station_id')
    search_fields = ('station_name', 'station_id')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'measurement_date'


@admin.register(ZoningReview)
class ZoningReviewAdmin(admin.ModelAdmin):
    list_display = ('application_id', 'application_date', 'address', 'status')
    list_filter = ('application_date', 'status')
    search_fields = ('application_id', 'address', 'proposal_description')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'application_date'


@admin.register(NeighbourhoodProfile)
class NeighbourhoodProfileAdmin(admin.ModelAdmin):
    list_display = ('neighbourhood_id', 'census_year', 'population', 'households', 'median_income')
    list_filter = ('census_year',)
    search_fields = ('neighbourhood_id',)
    readonly_fields = ('imported_at',)


# ============================================================================
# ADDITIONAL ADMIN
# ============================================================================

@admin.register(ForestLandCover)
class ForestLandCoverAdmin(admin.ModelAdmin):
    list_display = ('feature_id', 'land_cover_type', 'area_sqm')
    list_filter = ('land_cover_type',)
    search_fields = ('feature_id', 'land_cover_type')
    readonly_fields = ('imported_at',)


@admin.register(CommitteeAdjustmentApplication)
class CommitteeAdjustmentApplicationAdmin(admin.ModelAdmin):
    list_display = ('application_number', 'application_date', 'address', 'application_type', 'status')
    list_filter = ('application_date', 'status', 'application_type')
    search_fields = ('application_number', 'address')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'application_date'
