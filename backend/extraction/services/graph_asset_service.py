"""
Graph Asset Service

Generates graph-based assets (nodes, edges, tags) for semantic search and GNN training.
Separate from tag-filtered assets used for trend analysis.

Directory structure: /data/graph-assets/{country}/{YYYY-MM}/
"""

import logging
from pathlib import Path
from datetime import datetime
from django.conf import settings
from api.models import PbfFile, AssetBundle
from extraction.services.asset_extractor import extract_assets_from_pbf
from extraction.services.regional_path_service import normalize_country_slug

logger = logging.getLogger(__name__)


class GraphAssetService:
    """
    Service for generating graph-based assets from monthly snapshots.
    
    Key differences from AssetGenerationService:
    - Uses /data/graph-assets/ instead of backend/data/asset_bundles/
    - Organized by country + month, NOT session_id
    - No tag filtering - extracts ALL data from monthly snapshot
    - For semantic search and GNN training, not trend analysis
    """
    
    def __init__(self):
        self.base_dir = Path(getattr(settings, 'GRAPH_ASSETS_DIR', '/data/graph-assets'))
        self.base_dir.mkdir(parents=True, exist_ok=True)
    
    def generate_graph_assets_from_monthly(self, monthly_pbf_id: str, country_name: str) -> dict:
        """
        Generate graph assets (nodes, edges, tags) from monthly snapshot.
        
        Args:
            monthly_pbf_id: UUID of monthly PbfFile
            country_name: Country name for directory organization
        
        Returns:
            {
                'success': bool,
                'asset_bundle_id': str,
                'bundle_path': str,
                'node_count': int,
                'way_count': int,
                'edge_count': int,
                'tag_count': int
            }
        """
        try:
            # Get monthly PBF file
            monthly_pbf = PbfFile.objects.get(id=monthly_pbf_id)
            
            if not Path(monthly_pbf.path).exists():
                return {
                    'success': False,
                    'error': f'Monthly PBF file not found: {monthly_pbf.path}'
                }
            
            # Extract year-month from PBF timestamp
            if monthly_pbf.min_timestamp:
                year_month = monthly_pbf.min_timestamp.strftime('%Y-%m')
            else:
                year_month = datetime.now().strftime('%Y-%m')
            
            # Create output directory: /data/graph-assets/{country}/{YYYY-MM}/
            country_dir = self.base_dir / normalize_country_slug(country_name)
            output_dir = country_dir / year_month
            output_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Generating graph assets for {country_name} ({year_month})")
            logger.info(f"Source PBF: {monthly_pbf.path}")
            logger.info(f"Output directory: {output_dir}")
            
            # Extract assets using existing asset_extractor
            import time
            start_time = time.time()
            
            stats = extract_assets_from_pbf(
                pbf_path=str(monthly_pbf.path),
                output_dir=output_dir
            )
            
            duration = time.time() - start_time
            
            # Create AssetBundle record
            asset_bundle = AssetBundle.objects.create(
                source_pbf=monthly_pbf,
                bundle_path=str(output_dir),
                node_count=stats.get('nodes', 0),
                way_count=stats.get('ways', 0),
                relation_count=stats.get('relations', 0),
                edge_count=stats.get('edges', 0),
                tag_count=stats.get('tags', 0),
                generation_time_seconds=duration
            )
            
            logger.info(f"Created AssetBundle {asset_bundle.id}: {output_dir}")
            logger.info(f"Stats: {stats}")
            
            return {
                'success': True,
                'asset_bundle_id': str(asset_bundle.id),
                'bundle_path': str(output_dir),
                'node_count': asset_bundle.node_count,
                'way_count': asset_bundle.way_count,
                'relation_count': asset_bundle.relation_count,
                'edge_count': asset_bundle.edge_count,
                'tag_count': asset_bundle.tag_count,
                'generation_time_seconds': duration
            }
            
        except PbfFile.DoesNotExist:
            logger.error(f"Monthly PBF file not found: {monthly_pbf_id}")
            return {
                'success': False,
                'error': f'Monthly PBF file not found: {monthly_pbf_id}'
            }
        except Exception as e:
            logger.error(f"Error generating graph assets: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def batch_generate_from_monthly_extracts(self, monthly_pbf_ids: list, country_name: str) -> dict:
        """
        Generate graph assets for multiple monthly extracts.
        
        Args:
            monthly_pbf_ids: List of monthly PbfFile UUIDs
            country_name: Country name
        
        Returns:
            {
                'success': bool,
                'total_processed': int,
                'successful': int,
                'failed': int,
                'asset_bundles': [...]
            }
        """
        results = {
            'success': True,
            'total_processed': len(monthly_pbf_ids),
            'successful': 0,
            'failed': 0,
            'asset_bundles': []
        }
        
        for pbf_id in monthly_pbf_ids:
            result = self.generate_graph_assets_from_monthly(pbf_id, country_name)
            
            if result.get('success'):
                results['successful'] += 1
                results['asset_bundles'].append({
                    'pbf_id': pbf_id,
                    'asset_bundle_id': result['asset_bundle_id'],
                    'bundle_path': result['bundle_path']
                })
            else:
                results['failed'] += 1
                logger.error(f"Failed to generate assets for {pbf_id}: {result.get('error')}")
        
        results['success'] = results['failed'] == 0
        
        return results
