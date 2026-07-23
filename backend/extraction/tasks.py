"""
Celery tasks for async processing of snapshot generation and asset extraction
"""
from celery import shared_task, group, chord
from celery.utils.log import get_task_logger
from pathlib import Path
import time

from api.models import PbfFile, TemporalSnapshot, AssetBundle, ProcessingSession
from extraction.services.optimized_snapshot_service import OptimizedSnapshotService
from extraction.services.asset_generation_service import AssetGenerationService

logger = get_task_logger(__name__)


@shared_task(bind=True, time_limit=3600, soft_time_limit=3300)
def process_single_monthly_extract(self, monthly_pbf_id, temp_dir, session_dir, 
                                   region_name, tag_keys, asset_name, cpu_cores):
    """
    Process a single monthly extract with all tag keys.
    
    This task runs in parallel with other monthly extracts.
    Time limit: 1 hour per file (3600 seconds)
    """
    try:
        logger.info(f"Task {self.request.id}: Processing monthly PBF {monthly_pbf_id}")
        
        # Get PbfFile
        monthly_pbf = PbfFile.objects.get(id=monthly_pbf_id)
        
        # Create service instance
        service = OptimizedSnapshotService()
        
        # Process this monthly extract
        result = service._process_single_snapshot(
            monthly_pbf=monthly_pbf,
            temp_dir=Path(temp_dir),
            session_dir=Path(session_dir),
            region_name=region_name,
            tag_keys=tag_keys,
            asset_name=asset_name,
            cpu_cores=cpu_cores,
            generate_assets=True
        )
        
        if result:
            logger.info(f"Task {self.request.id}: Successfully processed {monthly_pbf_id}")
            logger.info(f"  Created {result['asset_bundles_created']} AssetBundles")
            return result
        else:
            logger.error(f"Task {self.request.id}: Failed to process {monthly_pbf_id}")
            return None
            
    except Exception as e:
        logger.error(f"Task {self.request.id}: Error processing {monthly_pbf_id}: {e}", exc_info=True)
        # Retry up to 3 times with exponential backoff
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries), max_retries=3)


@shared_task(bind=True)
def cleanup_temp_directory(self, temp_dir):
    """
    Cleanup task that runs after all extractions complete.
    """
    import shutil
    try:
        logger.info(f"Cleaning up temp directory: {temp_dir}")
        shutil.rmtree(temp_dir)
        logger.info(f"Successfully cleaned up {temp_dir}")
        return {'success': True, 'temp_dir': temp_dir}
    except Exception as e:
        logger.error(f"Failed to cleanup {temp_dir}: {e}")
        return {'success': False, 'error': str(e)}


@shared_task(bind=True)
def finalize_session(self, session_id, results):
    """
    Finalize the processing session after all tasks complete.
    Updates session status and aggregates results.
    """
    try:
        session = ProcessingSession.objects.get(id=session_id)
        
        # Filter out None results (failed tasks)
        successful_results = [r for r in results if r is not None]
        
        total_snapshots = len(successful_results)
        total_asset_bundles = sum(r.get('asset_bundles_created', 0) for r in successful_results)
        
        session.status = 'COMPLETED'
        session.results = {
            'snapshots_processed': total_snapshots,
            'asset_bundles_created': total_asset_bundles,
            'snapshots': successful_results
        }
        session.save()
        
        logger.info(f"Session {session_id} finalized: {total_snapshots} snapshots, {total_asset_bundles} AssetBundles")
        
        return {
            'success': True,
            'session_id': session_id,
            'snapshots_processed': total_snapshots,
            'asset_bundles_created': total_asset_bundles
        }
        
    except Exception as e:
        logger.error(f"Failed to finalize session {session_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


@shared_task
def generate_optimized_snapshots_async(config):
    """
    Main task that orchestrates parallel processing using Celery.
    
    Uses chord pattern: parallel tasks → callback
    """
    from django.utils import timezone
    
    try:
        # Get monthly PBF files
        monthly_pbf_files = PbfFile.objects.filter(
            id__in=config['monthly_pbf_ids']
        ).order_by('min_timestamp')
        
        session_id = config.get('session_id')
        region_name = config.get('region_name')
        tag_keys = config.get('tag_keys', [])
        asset_name = config.get('name', None)
        cpu_cores = config.get('cpu_cores', '8-27')
        
        # Setup directories
        from django.conf import settings
        service = OptimizedSnapshotService()
        output_dir = Path(config.get('output_directory', settings.ASSET_BUNDLES_DIR))
        session_dir = output_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        
        temp_dir = service.temp_dir / f'session_{session_id}'
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Starting async processing for session {session_id}")
        logger.info(f"Processing {monthly_pbf_files.count()} monthly extracts with {len(tag_keys)} tag keys")
        
        # Create parallel tasks for each monthly extract
        tasks = []
        for monthly_pbf in monthly_pbf_files:
            task = process_single_monthly_extract.s(
                monthly_pbf_id=str(monthly_pbf.id),
                temp_dir=str(temp_dir),
                session_dir=str(session_dir),
                region_name=region_name,
                tag_keys=tag_keys,
                asset_name=asset_name,
                cpu_cores=cpu_cores
            )
            tasks.append(task)
        
        # Use chord: run all tasks in parallel, then finalize
        callback = finalize_session.s(session_id=session_id)
        
        # Also cleanup temp directory after finalization
        workflow = chord(tasks)(callback) | cleanup_temp_directory.s(temp_dir=str(temp_dir))
        
        logger.info(f"Dispatched {len(tasks)} parallel tasks for session {session_id}")
        
        return {
            'success': True,
            'session_id': session_id,
            'tasks_dispatched': len(tasks),
            'workflow_id': workflow.id
        }
        
    except Exception as e:
        logger.error(f"Error in generate_optimized_snapshots_async: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


@shared_task(bind=True)
def extract_assets_from_pbf_async(self, pbf_path, output_dir, snapshot_id, 
                                  tag_key, asset_name, source_monthly_extract_id):
    """
    Async task for extracting assets from a single PBF file.
    
    This can be used independently or as part of the main workflow.
    """
    try:
        logger.info(f"Task {self.request.id}: Extracting assets from {pbf_path}")
        
        asset_service = AssetGenerationService()
        
        result = asset_service.generate_from_pbf({
            'pbf_path': pbf_path,
            'output_directory': output_dir,
            'snapshot_id': snapshot_id,
            'tag_key': tag_key,
            'name': asset_name,
            'source_monthly_extract_id': source_monthly_extract_id
        })
        
        if result['success']:
            logger.info(f"Task {self.request.id}: Successfully extracted assets")
            logger.info(f"  AssetBundle ID: {result['asset_bundle_id']}")
            logger.info(f"  Nodes: {result['node_count']:,}")
            logger.info(f"  Ways: {result['way_count']:,}")
        
        return result
        
    except Exception as e:
        logger.error(f"Task {self.request.id}: Error extracting assets: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=30, max_retries=2)


@shared_task(bind=True)
def generate_graph_assets_async(self, monthly_pbf_ids, country_name, max_workers=20):
    """
    Async task for generating graph assets from monthly extracts using multiprocessing.
    
    This task uses multiprocessing.Pool internally for maximum speed.
    """
    try:
        logger.info(f"Task {self.request.id}: Starting graph asset generation for {country_name}")
        logger.info(f"  Monthly extracts: {len(monthly_pbf_ids)}")
        logger.info(f"  Workers: {max_workers}")
        
        from extraction.services.graph_asset_parallel_service import GraphAssetParallelService
        
        service = GraphAssetParallelService()
        result = service.batch_generate_parallel(
            monthly_pbf_ids=monthly_pbf_ids,
            country_name=country_name,
            max_workers=max_workers
        )
        
        if result['success']:
            logger.info(f"Task {self.request.id}: Successfully generated graph assets")
            logger.info(f"  Total processed: {result['total_processed']}")
            logger.info(f"  New: {result['new']}")
            logger.info(f"  Skipped: {result['skipped']}")
            logger.info(f"  Duration: {result['duration_seconds']:.1f}s")
        else:
            logger.error(f"Task {self.request.id}: Graph asset generation failed: {result.get('error')}")
        
        return result
        
    except Exception as e:
        logger.error(f"Task {self.request.id}: Error generating graph assets: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60, max_retries=2)
