from django.contrib import admin
from .models import (
    PbfFile, Task, OsmiumDatasetMetrics
)


@admin.register(PbfFile)
class PbfFileAdmin(admin.ModelAdmin):
    list_display = ('path', 'pbf_file_type', 'status', 'size_mb', 'registered_at')
    list_filter = ('pbf_file_type', 'status', 'has_history')
    search_fields = ('path', 'source_url')
    readonly_fields = (
        'id', 'size_bytes', 'created_at', 'has_history', 'format_version',
        'file_type', 'compression', 'generator', 'raw_info', 'registered_at'
    )
    
    def size_mb(self, obj):
        if obj.size_bytes:
            return f"{obj.size_bytes / (1024 * 1024):.1f} MB"
        return "N/A"
    size_mb.short_description = 'Size'


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('task_type', 'status', 'created_at', 'updated_at')
    list_filter = ('task_type', 'status')
    readonly_fields = ('id', 'task_type', 'status', 'parameters', 'result', 'created_at', 'updated_at')


@admin.register(OsmiumDatasetMetrics)
class OsmiumDatasetMetricsAdmin(admin.ModelAdmin):
    list_display = ('pbf_file', 'metrics_generated_at', 'unique_tag_count')
    search_fields = ('pbf_file__path',)
    readonly_fields = [field.name for field in OsmiumDatasetMetrics._meta.fields]

    def has_add_permission(self, request):
        return False
