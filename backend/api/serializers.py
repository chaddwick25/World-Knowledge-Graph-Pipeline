import os
from rest_framework import serializers
from .models import (
    Task, PbfFile, RegionHierarchy, PolygonFile
)
# NOTE: ProjectionWeightAsset imported lazily inside ProjectionWeightAssetSerializer

class RegionHierarchySerializer(serializers.ModelSerializer):
    """Recursive serializer for the RegionHierarchy model."""
    children = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = RegionHierarchy
        fields = ['id', 'name', 'is_available', 'children', 'parent', 'poly_file_path', 'corresponding_pbf']

    def get_children(self, obj):
        # Recursively serialize children
        children = obj.children.all()
        serializer = RegionHierarchySerializer(children, many=True, context=self.context)
        return serializer.data

    def get_is_available(self, obj):
        # Determine if a region is available for processing
        # A region is available if its corresponding PBF is generated, OR if its parent is available.
        if obj.corresponding_pbf is not None:
            return True
        
        # For top-level nodes (continents), availability depends on the planet file.
        if obj.parent is None:
            return self.context.get('planet_file_available', False)
            
        # For other nodes, availability is inherited from the parent.
        # This logic is handled by the view, which passes down availability.
        # The serializer just reflects the pre-calculated value.
        return self.context.get(f'is_available_{obj.id}', False)


class PbfFileSerializer(serializers.ModelSerializer):
    region = serializers.SerializerMethodField()
    pbf_file_type_display = serializers.CharField(source='get_pbf_file_type_display', read_only=True)
    extraction_level_display = serializers.CharField(source='get_extraction_level_display', read_only=True)
    temporal_metadata_source_display = serializers.CharField(source='get_temporal_metadata_source_display', read_only=True)
    temporal_range = serializers.SerializerMethodField()
    extraction_hierarchy = serializers.SerializerMethodField()
    parent_pbf_path = serializers.SerializerMethodField()

    class Meta:
        model = PbfFile
        fields = [
            'id',
            'region',
            'pbf_file_type',
            'pbf_file_type_display',
            'min_timestamp',
            'max_timestamp',
            'temporal_range',
            'parent_pbf',
            'parent_pbf_path',
            'extraction_level',
            'extraction_level_display',
            'temporal_metadata_source',
            'temporal_metadata_source_display',
            'extraction_hierarchy',
            'source_url',
            'path',
            'status',
            'size_bytes',
            'created_at',
            'has_history',
            'format_version',
            'file_type',
            'compression',
            'generator',
            'raw_info',
            'registered_at',
            'yearly_extracts_generated',
            'yearly_extracts_completed_at',
            'yearly_extracts_count',
            'yearly_extracts_year_range',
            'monthly_extracts_generated',
            'monthly_extracts_completed_at',
            'monthly_extracts_count',
            'monthly_extracts_year_range',
        ]

    def get_region(self, obj):
        if obj.source_url:
            try:
                # Extract the filename from the URL
                file_name = os.path.basename(obj.source_url)
                # Clean up the name to get the region
                region_name = file_name.replace('-latest.osm.pbf', '').replace('_', ' ').title()
                return region_name
            except Exception:
                return "Unknown"
        return "Local File"
    
    def get_temporal_range(self, obj):
        """Get computed temporal range with fallback logic"""
        return obj.get_temporal_range()
    
    def get_extraction_hierarchy(self, obj):
        """Get full extraction hierarchy path"""
        return obj.get_extraction_hierarchy()
    
    def get_parent_pbf_path(self, obj):
        """Get parent PBF file path for display"""
        if obj.parent_pbf:
            return obj.parent_pbf.path
        return None


class TaskSerializer(serializers.ModelSerializer):
    task_type = serializers.CharField(source='get_task_type_display')
    status = serializers.CharField(source='get_status_display')

    class Meta:
        model = Task
        fields = ('id', 'task_type', 'status', 'parameters', 'result', 'created_at', 'updated_at')


class PolygonFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PolygonFile
        fields = '__all__'


class ProjectionWeightAssetSerializer(serializers.ModelSerializer):
    class Meta:
        from core.models import ProjectionWeightAsset
        model = ProjectionWeightAsset
        fields = '__all__'


