from django.contrib import admin

from .models import PipelineRun


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    """Read-only run tracking (written by the pipeline / dispatch services)."""
    list_display = (
        "country_code", "country_name", "pipeline_type", "status",
        "current_stage", "queued_at", "completed_at",
    )
    list_filter = ("pipeline_type", "status")
    search_fields = ("country_name", "country_code", "id")
    readonly_fields = [
        f.name for f in PipelineRun._meta.fields
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
