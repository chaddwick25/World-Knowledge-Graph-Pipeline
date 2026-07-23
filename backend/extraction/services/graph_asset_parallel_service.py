"""
Graph Asset Parallel Service

Generates graph assets from monthly extracts using multiprocessing for parallelism.
Follows the ParallelSnapshotService pattern but without tag filtering.

Author: EDA Vector Search Toolkit
"""
import logging
from pathlib import Path
from multiprocessing import Pool
from functools import partial
from django.conf import settings
import time
from extraction.services.regional_path_service import normalize_country_slug

logger = logging.getLogger(__name__)


def _process_graph_asset_worker(monthly_pbf, output_base_dir, country_name):
    """
    Worker function for multiprocessing.Pool.
    Processes a single monthly extract to generate graph assets.
    
    Must be a module-level function for pickle serialization.
    """
    import django
    django.setup()
    
    
    from api.models import PbfFile, AssetBundle
    from extraction.services.asset_extractor import extract_assets_from_pbf
    from pathlib import Path
    import logging
    import time
    
    logger = logging.getLogger(__name__)
    
    try:
        # Reload PbfFile in this process
        pbf = PbfFile.objects.get(id=monthly_pbf['id'])
        
        if not pbf.min_timestamp:
            logger.warning(f"Skipping {pbf.id}: no timestamp")
            return None
        
        # Create output directory: /data/graph-assets/{country}/{YYYY-MM}/
        year_month = pbf.min_timestamp.strftime('%Y-%m')
        output_dir = Path(output_base_dir) / normalize_country_slug(country_name) / year_month
        output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Worker processing: {pbf.path} → {output_dir}")
        
        # Create TemporalSnapshot to satisfy DB constraint
        from api.models import TemporalSnapshot
        snapshot, _ = TemporalSnapshot.objects.get_or_create(
            region=country_name,
            timestamp=pbf.min_timestamp,
            snapshot_interval='MONTHLY',
            filter_hash=None,
            defaults={'pbf_file': pbf}
        )
        
        # Check if AssetBundle already exists
        existing = AssetBundle.objects.filter(
            source_monthly_extract=pbf,
            bundle_path=str(output_dir)
        ).first()
        
        if existing:
            logger.info(f"AssetBundle already exists for {year_month}, skipping")
            return {
                'success': True,
                'asset_bundle_id': str(existing.id),
                'bundle_path': str(output_dir),
                'skipped': True
            }
        
        # Extract assets
        start_time = time.time()
        stats = extract_assets_from_pbf(
            pbf_path=str(pbf.path),
            output_dir=output_dir
        )
        duration = time.time() - start_time
        
        # Calculate total size of Parquet assets
        total_size = sum(f.stat().st_size for f in output_dir.glob('*.parquet'))
        
        # DEBUG: Trace exactly what is being sent to prevent the NotNullViolation
        logger.error(f"DEBUG: Before AssetBundle.create - snapshot: {snapshot}")
        logger.error(f"DEBUG: snapshot.id: {snapshot.id if snapshot else 'NONE_SNAPSHOT'}")
        logger.error(f"DEBUG: snapshot_interval: {snapshot.snapshot_interval}")
        logger.error(f"DEBUG: total_size variable evaluated to: {total_size}")
        
        # Create AssetBundle record ensuring the foreign_key id works directly
        asset_bundle = AssetBundle.objects.create(
            source_monthly_extract_id=pbf.id,
            temporal_snapshot_id=snapshot.id if snapshot else None,
            bundle_path=str(output_dir),
            node_count=stats.get('nodes', 0),
            way_count=stats.get('ways', 0),
            relation_count=stats.get('relations', 0),
            edge_count=stats.get('edges', 0),
            tag_count=stats.get('tags', 0),
            generation_time_seconds=duration,
            total_size_bytes=total_size
        )
        
        logger.info(f"Created AssetBundle {asset_bundle.id} for {year_month}")
        logger.info(f"  Nodes: {stats.get('nodes', 0):,}, Ways: {stats.get('ways', 0):,}, Edges: {stats.get('edges', 0):,}")
        
        return {
            'success': True,
            'asset_bundle_id': str(asset_bundle.id),
            'bundle_path': str(output_dir),
            'year_month': year_month,
            'stats': stats,
            'duration': duration
        }
        
    except Exception as e:
        logger.error(f"Worker failed for {monthly_pbf.get('id')}: {e}", exc_info=True)
        return None


class GraphAssetParallelService:
    """
    Service for generating graph assets from monthly extracts using multiprocessing.
    
    Similar to ParallelSnapshotService but:
    - No tag filtering (extracts ALL data)
    - No TemporalSnapshot records
    - Direct monthly extract → asset bundle
    """
    
    def __init__(self):
        default_path = str(Path(settings.BASE_DATA_DIR) / 'graph-assets') if settings.BASE_DATA_DIR else 'backend/data/graph-assets'
        self.base_dir = Path(getattr(settings, 'GRAPH_ASSETS_DIR', default_path))
        self.base_dir.mkdir(parents=True, exist_ok=True)
    
    def batch_generate_parallel(self, monthly_pbf_ids, country_name, max_workers=20):
        """
        Generate graph assets for multiple monthly extracts in parallel.
        
        Args:
            monthly_pbf_ids: List of PbfFile UUIDs
            country_name: Country name for directory structure
            max_workers: Number of parallel workers (default: 20)
        
        Returns:
            {
                'success': bool,
                'total_processed': int,
                'successful': int,
                'failed': int,
                'skipped': int,
                'asset_bundles': [...]
            }
        """
        from api.models import PbfFile
        
        try:
            # Query PbfFile records
            monthly_pbfs = PbfFile.objects.filter(id__in=monthly_pbf_ids).order_by('min_timestamp')
            
            if not monthly_pbfs.exists():
                return {
                    'success': False,
                    'error': 'No monthly PBF files found'
                }
            
            logger.info(f"Starting parallel graph asset generation for {monthly_pbfs.count()} monthly extracts")
            logger.info(f"Country: {country_name}")
            logger.info(f"Workers: {max_workers}")
            logger.info(f"Output: {self.base_dir}")
            
            # Convert QuerySet to list of dicts for multiprocessing
            pbf_list = [
                {
                    'id': str(pbf.id),
                    'path': pbf.path,
                    'timestamp': pbf.min_timestamp.isoformat() if pbf.min_timestamp else None
                }
                for pbf in monthly_pbfs
            ]
            
            # Create partial function with fixed arguments
            process_func = partial(
                _process_graph_asset_worker,
                output_base_dir=str(self.base_dir),
                country_name=country_name
            )
            
            # Process in parallel
            logger.info(f"Dispatching {len(pbf_list)} tasks to {max_workers} worker processes")
            start_time = time.time()
            
            # Close connections in PARENT before fork to ensure children don't inherit FDs
            # This completely avoids Postgres OperatorErrors or severed sockets
            from django.db import connections
            connections.close_all()
            
            with Pool(processes=max_workers) as pool:
                results = pool.map(process_func, pbf_list)
            
            duration = time.time() - start_time
            
            # Analyze results
            successful_results = [r for r in results if r and r.get('success')]
            skipped_results = [r for r in successful_results if r.get('skipped')]
            new_results = [r for r in successful_results if not r.get('skipped')]
            failed_count = len([r for r in results if not r or not r.get('success')])
            
            logger.info(f"Parallel processing completed in {duration:.1f}s")
            logger.info(f"  Total: {len(results)}")
            logger.info(f"  New: {len(new_results)}")
            logger.info(f"  Skipped: {len(skipped_results)}")
            logger.info(f"  Failed: {failed_count}")
            
            return {
                'success': failed_count == 0,
                'total_processed': len(results),
                'successful': len(successful_results),
                'new': len(new_results),
                'skipped': len(skipped_results),
                'failed': failed_count,
                'asset_bundles': successful_results,
                'duration_seconds': duration
            }
            
        except Exception as e:
            logger.error(f"Parallel graph asset generation failed: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
