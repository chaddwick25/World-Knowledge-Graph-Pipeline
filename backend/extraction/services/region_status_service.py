"""
Region Status Aggregation Service

Aggregates status information for regions including:
- PBF file status (exists, size, timestamps)
- Extraction status (pending, in_progress, completed, failed)
- Downstream task status (embeddings, GNN training, asset bundles)
"""

import logging
from typing import Dict, List, Optional
from django.db.models import Q, Count, Sum
from extraction.models import PbfFile, RegionHierarchy, PolygonFile
from orchestration.models import ProcessingSession, Task

logger = logging.getLogger(__name__)


class RegionStatusService:
    """Service for aggregating region status information"""
    
    @staticmethod
    def get_region_status(region_hierarchy: RegionHierarchy) -> Dict:
        """
        Get comprehensive status for a single region.
        
        Args:
            region_hierarchy: RegionHierarchy instance
            
        Returns:
            dict: Complete status information
        """
        status = {
            'region_id': str(region_hierarchy.id),
            'name': region_hierarchy.name,
            'pbf_status': RegionStatusService._get_pbf_status(region_hierarchy),
            'extraction_status': RegionStatusService._get_extraction_status(region_hierarchy),
            'downstream_tasks': RegionStatusService._get_downstream_task_status(region_hierarchy)
        }
        
        # Calculate overall completion percentage
        status['completion_percent'] = RegionStatusService._calculate_completion_percent(status)
        
        return status
    
    @staticmethod
    def _get_pbf_status(region_hierarchy: RegionHierarchy) -> Dict:
        """Get PBF file status for a region"""
        pbf = region_hierarchy.corresponding_pbf
        
        if not pbf:
            return {
                'exists': False,
                'status': 'not_extracted'
            }
        
        return {
            'exists': True,
            'file_size_bytes': pbf.size_bytes,
            'file_size_gb': round(pbf.size_bytes / (1024**3), 2) if pbf.size_bytes else 0,
            'extraction_level': pbf.extraction_level,
            'last_updated': pbf.registered_at.isoformat() if pbf.registered_at else None,
            'status': pbf.status,
            'has_history': pbf.has_history,
            'temporal_range': {
                'min': pbf.min_timestamp.isoformat() if pbf.min_timestamp else None,
                'max': pbf.max_timestamp.isoformat() if pbf.max_timestamp else None
            }
        }
    
    @staticmethod
    def _get_extraction_status(region_hierarchy: RegionHierarchy) -> Dict:
        """Get extraction status for a region"""
        pbf = region_hierarchy.corresponding_pbf
        
        if not pbf:
            return {
                'yearly_extracts': False,
                'monthly_extracts': False,
                'temporal_range': None
            }
        
        return {
            'yearly_extracts': pbf.yearly_extracts_generated,
            'yearly_count': pbf.yearly_extracts_count,
            'monthly_extracts': pbf.monthly_extracts_generated,
            'monthly_count': pbf.monthly_extracts_count,
            'temporal_range': pbf.yearly_extracts_year_range
        }
    
    @staticmethod
    def _get_downstream_task_status(region_hierarchy: RegionHierarchy) -> Dict:
        """
        Get downstream task status for a region.
        
        Checks for:
        - Semantic search (GV-Tags/GV-NLE embeddings)
        - GNN training status
        - Asset bundles
        """
        pbf = region_hierarchy.corresponding_pbf
        
        if not pbf:
            return {
                'semantic_search': {'available': False},
                'gnn_training': {'available': False},
                'asset_bundles': {'available': False}
            }
        
        # Check for semantic search embeddings
        semantic_status = RegionStatusService._check_semantic_search_status(pbf)
        
        # Check for GNN training
        gnn_status = RegionStatusService._check_gnn_training_status(pbf)
        
        # Check for asset bundles
        asset_status = RegionStatusService._check_asset_bundle_status(pbf)
        
        return {
            'semantic_search': semantic_status,
            'gnn_training': gnn_status,
            'asset_bundles': asset_status
        }
    
    @staticmethod
    def _check_semantic_search_status(pbf: PbfFile) -> Dict:
        """Check if semantic search embeddings exist for this PBF"""
        try:
            from worldkg_nca.models import OsmEntity
            
            # Check if any entities exist for this PBF
            entity_count = OsmEntity.objects.using('vectors').filter(
                source_snapshot_id=str(pbf.id)
            ).count()
            
            # Check for GV-Tags and GV-NLE embeddings
            gv_tags_count = OsmEntity.objects.using('vectors').filter(
                source_snapshot_id=str(pbf.id),
                gv_tags_embedding__isnull=False
            ).count()
            
            gv_nle_count = OsmEntity.objects.using('vectors').filter(
                source_snapshot_id=str(pbf.id),
                gv_nle_embedding__isnull=False
            ).count()
            
            return {
                'available': entity_count > 0,
                'entity_count': entity_count,
                'gv_tags_available': gv_tags_count > 0,
                'gv_tags_count': gv_tags_count,
                'gv_nle_available': gv_nle_count > 0,
                'gv_nle_count': gv_nle_count
            }
        except Exception as e:
            logger.warning(f"Could not check semantic search status: {str(e)}")
            return {'available': False}
    
    @staticmethod
    def _check_gnn_training_status(pbf: PbfFile) -> Dict:
        """Check GNN training status for this PBF"""
        # This would check for trained models in the database
        return {
            'available': False,
            'gcn_trained': False,
            'graphsage_trained': False,
            'gat_trained': False
        }
    
    @staticmethod
    def _check_asset_bundle_status(pbf: PbfFile) -> Dict:
        """Check if asset bundles exist for this PBF"""
        try:
            from api.models import AssetBundle
            
            # Check for asset bundles
            bundles = AssetBundle.objects.filter(
                temporal_snapshot__pbf_file=pbf
            )
            
            bundle_count = bundles.count()
            
            if bundle_count > 0:
                # Get unique tag keys
                tag_keys = list(bundles.values_list('tag_key', flat=True).distinct())
                
                return {
                    'available': True,
                    'bundle_count': bundle_count,
                    'tag_keys': tag_keys
                }
            
            return {'available': False}
            
        except Exception as e:
            logger.warning(f"Could not check asset bundle status: {str(e)}")
            return {'available': False}
    
    @staticmethod
    def _calculate_completion_percent(status: Dict) -> int:
        """
        Calculate overall completion percentage for a region.
        
        Scoring:
        - PBF exists: 40%
        - Yearly extracts: 20%
        - Semantic embeddings: 20%
        - GNN training: 10%
        - Asset bundles: 10%
        """
        percent = 0
        
        # PBF exists
        if status['pbf_status']['exists']:
            percent += 40
        
        # Yearly extracts
        if status['extraction_status']['yearly_extracts']:
            percent += 20
        
        # Semantic embeddings
        if status['downstream_tasks']['semantic_search']['available']:
            percent += 20
        
        # GNN training
        if status['downstream_tasks']['gnn_training']['available']:
            percent += 10
        
        # Asset bundles
        if status['downstream_tasks']['asset_bundles']['available']:
            percent += 10
        
        return percent
    
    @staticmethod
    def aggregate_hierarchy_status() -> List[Dict]:
        """
        Aggregate status for entire region hierarchy.
        
        Returns:
            list: Hierarchical structure with status for all regions
        """
        # Get top-level regions (continents)
        top_level_regions = RegionHierarchy.objects.filter(
            region_type=RegionHierarchy.RegionType.CONTINENT
        ).prefetch_related('children', 'corresponding_pbf', 'polygon_file')
        
        result = []
        for region in top_level_regions:
            region_data = RegionStatusService._build_region_tree(region)
            result.append(region_data)
        
        return result
    
    @staticmethod
    def _build_region_tree(region: RegionHierarchy) -> Dict:
        """
        Recursively build region tree with status.
        
        Args:
            region: RegionHierarchy instance
            
        Returns:
            dict: Region data with children
        """
        # Get status for this region
        status = RegionStatusService.get_region_status(region)
        
        # Add basic info
        region_data = {
            'id': str(region.id),
            'name': region.name,
            'level': RegionStatusService._determine_level(region),
            'status': status,
            'children': []
        }
        
        # Recursively add children
        for child in region.children.all():
            child_data = RegionStatusService._build_region_tree(child)
            region_data['children'].append(child_data)
        
        return region_data
    
    @staticmethod
    def _determine_level(region: RegionHierarchy) -> str:
        """
        Determine the hierarchical level of a region.
        
        Returns: 'continent', 'country', 'region', or 'city'
        """
        # Count ancestors to determine level
        depth = 0
        current = region
        while current.parent:
            depth += 1
            current = current.parent
        
        levels = {
            0: 'continent',
            1: 'country',
            2: 'region',
            3: 'city'
        }
        
        return levels.get(depth, 'subregion')
    
    @staticmethod
    def get_global_statistics() -> Dict:
        """
        Get planet-wide statistics.
        
        Returns:
            dict: Global statistics
        """
        total_regions = RegionHierarchy.objects.count()
        total_pbfs = PbfFile.objects.count()
        total_size_bytes = PbfFile.objects.aggregate(
            total=Sum('size_bytes')
        )['total'] or 0
        
        # Count regions by status
        regions_with_pbf = RegionHierarchy.objects.filter(
            corresponding_pbf__isnull=False
        ).count()
        
        return {
            'total_regions': total_regions,
            'total_pbf_files': total_pbfs,
            'regions_extracted': regions_with_pbf,
            'regions_pending': total_regions - regions_with_pbf,
            'total_size_gb': round(total_size_bytes / (1024**3), 2),
            'extraction_coverage_percent': round(
                (regions_with_pbf / total_regions * 100) if total_regions > 0 else 0,
                1
            )
        }
