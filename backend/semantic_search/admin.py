from django.contrib import admin
from .models import (
    ProjectionTrainingPair,
    ProjectionHeadCheckpoint,
    EmbeddingComparison,
    TrainingMetrics
)


@admin.register(ProjectionTrainingPair)
class ProjectionTrainingPairAdmin(admin.ModelAdmin):
    list_display = ['id', 'query_text_preview', 'ground_truth_tract', 'created_at']
    list_filter = ['created_at']
    search_fields = ['query_text', 'tag_counts']
    readonly_fields = ['id', 'created_at']
    
    def query_text_preview(self, obj):
        return obj.query_text[:50] + '...' if len(obj.query_text) > 50 else obj.query_text
    query_text_preview.short_description = 'Query Text'


@admin.register(ProjectionHeadCheckpoint)
class ProjectionHeadCheckpointAdmin(admin.ModelAdmin):
    list_display = ['id', 'version', 'epoch', 'train_loss', 'val_loss', 'created_at']
    list_filter = ['version', 'created_at']
    search_fields = ['version']
    readonly_fields = ['id', 'created_at']
    ordering = ['-created_at']


@admin.register(EmbeddingComparison)
class EmbeddingComparisonAdmin(admin.ModelAdmin):
    list_display = ['id', 'test_name', 'method', 'top_1_similarity', 'inference_time_ms', 'timestamp']
    list_filter = ['method', 'test_name', 'timestamp']
    search_fields = ['test_name', 'query_tags']
    readonly_fields = ['id', 'timestamp']
    ordering = ['-timestamp']


@admin.register(TrainingMetrics)
class TrainingMetricsAdmin(admin.ModelAdmin):
    list_display = ['id', 'checkpoint', 'epoch', 'batch_idx', 'loss_total', 'learning_rate', 'timestamp']
    list_filter = ['checkpoint', 'epoch']
    readonly_fields = ['id', 'timestamp']
    ordering = ['checkpoint', 'epoch', 'batch_idx']
