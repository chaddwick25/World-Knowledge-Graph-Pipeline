from rest_framework import serializers
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


class QualitySnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = QualitySnapshot
        fields = [
            'id', 'quality_score_pct', 'grade', 'freshness',
            'metadata_score', 'usability', 'completeness',
            'accessibility', 'qa_recorded_at', 'ingested_at'
        ]


class CkanResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CkanResource
        fields = [
            'id', 'ckan_resource_id', 'name', 'format', 'url',
            'size', 'mimetype', 'last_modified', 'last_downloaded_at'
        ]


class CkanDatasetSerializer(serializers.ModelSerializer):
    resources = CkanResourceSerializer(many=True, read_only=True)
    latest_quality = serializers.SerializerMethodField()
    
    class Meta:
        model = CkanDataset
        fields = [
            'id', 'ckan_id', 'title', 'name', 'notes',
            'refresh_rate', 'is_retired', 'owner_org',
            'metadata_created', 'metadata_modified',
            'last_synced_at', 'resources', 'latest_quality'
        ]
    
    def get_latest_quality(self, obj):
        latest = obj.snapshots.order_by('-ingested_at').first()
        if latest:
            return QualitySnapshotSerializer(latest).data
        return None


class CkanDatasetListSerializer(serializers.ModelSerializer):
    """Simplified serializer for list views"""
    latest_quality = serializers.SerializerMethodField()
    resource_count = serializers.SerializerMethodField()
    
    class Meta:
        model = CkanDataset
        fields = [
            'id', 'ckan_id', 'title', 'refresh_rate',
            'is_retired', 'last_synced_at', 'latest_quality',
            'resource_count'
        ]
    
    def get_latest_quality(self, obj):
        latest = obj.snapshots.order_by('-ingested_at').first()
        if latest:
            return {
                'grade': latest.grade,
                'score': latest.quality_score_pct,
                'qa_recorded_at': latest.qa_recorded_at
            }
        return None
    
    def get_resource_count(self, obj):
        return obj.resources.count()


class TrafficVolumeSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = TrafficVolume
        fields = [
            'id', 'intersection_id', 'location', 'date',
            'time_period', 'vehicle_count', 'pedestrian_count',
            'cyclist_count', 'latitude', 'longitude',
            'dataset_name', 'imported_at'
        ]


class TtcSubwayDelaySerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = TtcSubwayDelay
        fields = [
            'id', 'date', 'time', 'station', 'line',
            'delay_minutes', 'delay_code', 'delay_reason',
            'vehicle_number', 'dataset_name', 'imported_at'
        ]


class CafetoLocationSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = CafetoLocation
        fields = [
            'id', 'business_name', 'address', 'ward',
            'latitude', 'longitude', 'installation_date',
            'status', 'dataset_name', 'imported_at'
        ]


# ============================================================================
# SPATIAL INFRASTRUCTURE SERIALIZERS
# ============================================================================

class TorontoCentrelineSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = TorontoCentreline
        fields = [
            'id', 'centreline_id', 'linear_name_full', 'address_l', 'address_r',
            'from_intersection_id', 'to_intersection_id', 'feature_code',
            'geometry', 'dataset_name', 'imported_at'
        ]


class IntersectionFileSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = IntersectionFile
        fields = [
            'id', 'intersection_id', 'intersection_desc', 'latitude', 'longitude',
            'elevation', 'geometry', 'dataset_name', 'imported_at'
        ]


class CyclingNetworkSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = CyclingNetwork
        fields = [
            'id', 'segment_id', 'street_name', 'infrastructure_type',
            'from_street', 'to_street', 'length_m', 'geometry',
            'dataset_name', 'imported_at'
        ]


class NeighbourhoodSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = Neighbourhood
        fields = [
            'id', 'neighbourhood_id', 'neighbourhood_name', 'area_sqkm',
            'geometry', 'dataset_name', 'imported_at'
        ]


class ZoningByLawSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = ZoningByLaw
        fields = [
            'id', 'zone_id', 'zone_category', 'zone_label', 'description',
            'geometry', 'dataset_name', 'imported_at'
        ]


class BusinessImprovementAreaSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = BusinessImprovementArea
        fields = [
            'id', 'bia_id', 'bia_name', 'area_sqkm', 'geometry',
            'dataset_name', 'imported_at'
        ]


# ============================================================================
# TEMPORAL FLOW SERIALIZERS
# ============================================================================

class BicycleCounterSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = BicycleCounter
        fields = [
            'id', 'location_id', 'location_name', 'count_date', 'count_time',
            'count_value', 'latitude', 'longitude', 'dataset_name', 'imported_at'
        ]


class TtcRouteSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = TtcRoute
        fields = [
            'id', 'route_id', 'route_name', 'route_type', 'geometry',
            'dataset_name', 'imported_at'
        ]


class RainGaugeSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = RainGauge
        fields = [
            'id', 'station_id', 'station_name', 'measurement_date', 'measurement_time',
            'precipitation_mm', 'latitude', 'longitude', 'dataset_name', 'imported_at'
        ]


class ZoningReviewSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = ZoningReview
        fields = [
            'id', 'application_id', 'application_date', 'address', 'proposal_description',
            'status', 'latitude', 'longitude', 'dataset_name', 'imported_at'
        ]


class NeighbourhoodProfileSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = NeighbourhoodProfile
        fields = [
            'id', 'neighbourhood_id', 'census_year', 'population', 'households',
            'median_income', 'raw_data', 'dataset_name', 'imported_at'
        ]


# ============================================================================
# ADDITIONAL SERIALIZERS
# ============================================================================

class ForestLandCoverSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = ForestLandCover
        fields = [
            'id', 'feature_id', 'land_cover_type', 'area_sqm', 'geometry',
            'dataset_name', 'imported_at'
        ]


class CommitteeAdjustmentApplicationSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='resource.dataset.title', read_only=True)
    
    class Meta:
        model = CommitteeAdjustmentApplication
        fields = [
            'id', 'application_number', 'application_date', 'address', 'application_type',
            'status', 'latitude', 'longitude', 'dataset_name', 'imported_at'
        ]
