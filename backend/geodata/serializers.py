from rest_framework import serializers

from .models import (
    DataQualitySnapshot,
    DataSource,
    GeoDataset,
    GeoRecord,
    GeoResource,
)


class DataSourceSerializer(serializers.ModelSerializer):
    dataset_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = DataSource
        fields = [
            'id', 'name', 'adapter_type', 'base_url', 'country_code',
            'is_active', 'config', 'dataset_count', 'updated_at',
        ]


class GeoResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeoResource
        fields = [
            'id', 'source_resource_id', 'name', 'format', 'url',
            'size_bytes', 'mimetype', 'last_modified', 'last_downloaded_at',
        ]


class DataQualitySnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataQualitySnapshot
        fields = [
            'id', 'quality_score_pct', 'grade', 'freshness',
            'metadata_score', 'usability', 'completeness',
            'accessibility', 'qa_recorded_at', 'ingested_at',
        ]


class GeoDatasetSerializer(serializers.ModelSerializer):
    source = DataSourceSerializer(read_only=True)
    resources = GeoResourceSerializer(many=True, read_only=True)
    latest_quality = serializers.SerializerMethodField()
    record_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GeoDataset
        fields = [
            'id', 'source', 'source_dataset_id', 'title', 'name',
            'description', 'publisher', 'license', 'refresh_rate',
            'keywords', 'is_retired', 'last_synced_at',
            'resources', 'latest_quality', 'record_count',
        ]

    def get_latest_quality(self, obj):
        latest = obj.quality_snapshots.order_by('-ingested_at').first()
        if latest:
            return DataQualitySnapshotSerializer(latest).data
        return None


class GeoDatasetListSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source='source.name', read_only=True)
    latest_grade = serializers.SerializerMethodField()
    record_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GeoDataset
        fields = [
            'id', 'source_name', 'source_dataset_id', 'title', 'name',
            'refresh_rate', 'is_retired', 'last_synced_at',
            'latest_grade', 'record_count',
        ]

    def get_latest_grade(self, obj):
        latest = obj.quality_snapshots.order_by('-ingested_at').first()
        return latest.grade if latest else None


class GeoRecordSerializer(serializers.ModelSerializer):
    dataset = serializers.CharField(source='dataset.name', read_only=True)
    resource = serializers.CharField(source='resource.name', read_only=True)
    geometry = serializers.SerializerMethodField()

    class Meta:
        model = GeoRecord
        fields = [
            'id', 'dataset', 'resource', 'source_id',
            'attributes', 'geometry', 'ingestion_date', 'imported_at',
        ]

    def get_geometry(self, obj):
        if obj.geom is None:
            return None
        try:
            return obj.geom.geojson
        except Exception:
            return None
