from django.contrib import admin

from .models import (
    DataQualitySnapshot,
    DataSource,
    GeoDataset,
    GeoRecord,
    GeoResource,
    IngestionState,
)


class GeoResourceInline(admin.TabularInline):
    model = GeoResource
    extra = 0
    readonly_fields = ('created_at', 'last_downloaded_at')
    fields = ('name', 'format', 'size_bytes', 'last_modified', 'last_downloaded_at')


class DataQualitySnapshotInline(admin.TabularInline):
    model = DataQualitySnapshot
    extra = 0
    readonly_fields = ('ingested_at',)
    fields = ('grade', 'quality_score_pct', 'freshness', 'qa_recorded_at', 'ingested_at')


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'adapter_type', 'base_url', 'country_code', 'is_active', 'updated_at')
    list_filter = ('adapter_type', 'is_active', 'country_code')
    search_fields = ('name', 'base_url')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(GeoDataset)
class GeoDatasetAdmin(admin.ModelAdmin):
    list_display = ('name', 'title', 'source', 'refresh_rate', 'is_retired', 'last_synced_at')
    list_filter = ('source', 'is_retired', 'refresh_rate')
    search_fields = ('name', 'title', 'source_dataset_id')
    readonly_fields = ('last_synced_at', 'created_at')
    inlines = [GeoResourceInline, DataQualitySnapshotInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('source', 'source_dataset_id', 'name', 'title', 'description'),
        }),
        ('Metadata', {
            'fields': ('publisher', 'license', 'refresh_rate', 'keywords', 'is_retired'),
        }),
        ('Temporal', {
            'fields': ('spatial_extent', 'temporal_start', 'temporal_end'),
        }),
        ('Timestamps', {
            'fields': ('last_synced_at', 'created_at'),
        }),
    )


@admin.register(GeoResource)
class GeoResourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'dataset', 'format', 'size_bytes', 'last_modified', 'last_downloaded_at')
    list_filter = ('format', 'dataset__source')
    search_fields = ('name', 'source_resource_id', 'url')
    readonly_fields = ('created_at',)


@admin.register(DataQualitySnapshot)
class DataQualitySnapshotAdmin(admin.ModelAdmin):
    list_display = ('dataset', 'grade', 'quality_score_pct', 'freshness', 'qa_recorded_at', 'ingested_at')
    list_filter = ('grade', 'dataset__source')
    search_fields = ('dataset__name', 'dataset__title')
    readonly_fields = ('ingested_at',)
    date_hierarchy = 'qa_recorded_at'


@admin.register(IngestionState)
class IngestionStateAdmin(admin.ModelAdmin):
    list_display = (
        'resource', 'last_ingested_date', 'last_ingested_count',
        'total_records_ingested', 'last_ingestion_mode', 'last_ingestion_at',
    )
    list_filter = ('last_ingestion_mode',)
    search_fields = ('resource__name',)


@admin.register(GeoRecord)
class GeoRecordAdmin(admin.ModelAdmin):
    list_display = ('dataset', 'resource', 'source_id', 'ingestion_date', 'imported_at')
    list_filter = ('dataset',)
    search_fields = ('source_id', 'resource__name')
    readonly_fields = ('imported_at',)
    date_hierarchy = 'ingestion_date'
