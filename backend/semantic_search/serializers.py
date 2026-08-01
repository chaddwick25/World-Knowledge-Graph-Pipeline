from rest_framework import serializers
from .models import (
    ProjectionTrainingPair,
    ProjectionHeadCheckpoint,
    EmbeddingComparison,
    TrainingMetrics
)


class ProjectionTrainingPairSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectionTrainingPair
        fields = [
            'id', 'tag_counts', 'query_text', 'teacher_embedding',
            'ground_truth_tract', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ProjectionHeadCheckpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectionHeadCheckpoint
        fields = [
            'id', 'version', 'epoch', 'train_loss', 'val_loss',
            'config', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class EmbeddingComparisonSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmbeddingComparison
        fields = [
            'id', 'test_name', 'method', 'query_tags', 'query_text',
            'inference_time_ms', 'memory_mb', 'gpu_memory_mb',
            'top_5_results', 'top_1_similarity',
            'recall_at_5', 'recall_at_10', 'mrr', 'ndcg_at_10',
            'overlap_at_5', 'overlap_at_10', 'rank_correlation',
            'timestamp'
        ]
        read_only_fields = ['id', 'timestamp']


class TrainingMetricsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingMetrics
        fields = [
            'id', 'checkpoint', 'epoch', 'batch_idx',
            'loss_total', 'loss_alignment', 'loss_contrastive',
            'loss_rank_distillation', 'learning_rate',
            'gradient_norm', 'timestamp'
        ]
        read_only_fields = ['id', 'timestamp']


class SemanticSearchRequestSerializer(serializers.Serializer):
    """Serializer for semantic search API requests."""
    
    method = serializers.ChoiceField(
        choices=['fasttext', 'hidden_state'],
        default='fasttext',
        help_text="Embedding method to use: fasttext (CPU), hidden_state (future)"
    )
    tag_counts = serializers.JSONField(
        help_text="Tag counts dictionary, e.g., {'cafe': 40, 'residential': 30}"
    )
    filters = serializers.JSONField(
        required=False,
        default=dict,
        help_text="Filters for census tracts (country, region, etc.)"
    )
    top_k = serializers.IntegerField(
        default=10,
        min_value=1,
        max_value=100,
        help_text="Number of top results to return"
    )


class SemanticSearchResponseSerializer(serializers.Serializer):
    """Serializer for semantic search API responses."""
    
    method = serializers.CharField()
    query_tags = serializers.JSONField()
    query_text = serializers.CharField(required=False, allow_null=True)
    inference_time_ms = serializers.FloatField()
    results = serializers.ListField(
        child=serializers.DictField()
    )


class ComparisonRequestSerializer(serializers.Serializer):
    """Serializer for comparison API requests."""
    
    tag_counts = serializers.JSONField(
        help_text="Tag counts dictionary"
    )
    methods = serializers.ListField(
        child=serializers.ChoiceField(choices=['fasttext', 'hidden_state']),
        default=['fasttext', 'hidden_state'],
        help_text="Methods to compare"
    )
    filters = serializers.JSONField(
        required=False,
        default=dict
    )
    top_k = serializers.IntegerField(default=10, min_value=1, max_value=100)
