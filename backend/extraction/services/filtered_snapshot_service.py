"""
Filtered Snapshot Service

Generates filtered daily snapshots from monthly snapshots for trend analysis.
Reduces file sizes by filtering only target tags before creating daily snapshots.

Author: EDA Vector Search Toolkit
"""
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings
from api.models import TemporalSnapshot, AssetBundle, PbfFile
from extraction.services.osmium_facade import OsmiumFacade
from extraction.services.asset_generation_service import AssetGenerationService
import hashlib
import json

logger = logging.getLogger(__name__)


class FilteredSnapshotService:
    """
    Service for generating filtered daily snapshots from monthly snapshots.
    
    Workflow:
    1. Take monthly snapshot as source
    2. Filter by target tags (osmium tags-filter) -> 95%+ size reduction
    3. Generate daily snapshot at month-end (osmium time-filter)
    4. Create TemporalSnapshot record
    5. Generate AssetBundle for analysis
    """
    
    def __init__(self):
        self.osmium_facade = OsmiumFacade()
        self.asset_service = AssetGenerationService()
    
    def _generate_filter_hash(self, target_tags):
        """Generate MD5 hash from sorted target tags for uniqueness."""
        sorted_tags = sorted(target_tags)
        tag_string = json.dumps(sorted_tags, sort_keys=True)
        return hashlib.md5(tag_string.encode()).hexdigest()
    
    def generate_filtered_daily_snapshots(self, config):
        """
        Generate filtered daily snapshots from monthly snapshots.
        
        Args:
            config = {
                'monthly_pbf_ids': [uuid1, uuid2, ...],  # PbfFile IDs, not TemporalSnapshot
                'target_tags': ['amenity=bubble_tea', 'cuisine=bubble_tea'],
                'output_directory': 'backend/data/asset_bundles',
                'generate_assets': True,
                'session_id': 'uuid-of-trend-analysis-session',
                'region_name': 'ukraine'  # For metadata only
            }
        
        Returns:
            {
                'success': True,
                'daily_snapshots_created': 72,
                'total_size_mb': 360,
                'size_reduction_percent': 95,
                'snapshots': [...]
            }
        """
        try:
            # Query PbfFile records for monthly extracts
            monthly_pbf_files = PbfFile.objects.filter(
                id__in=config['monthly_pbf_ids']
            ).order_by('min_timestamp')
            
            if not monthly_pbf_files.exists():
                return {
                    'success': False,
                    'error': 'No monthly PBF files found'
                }
            
            output_dir = Path(config.get('output_directory', settings.ASSET_BUNDLES_DIR))
            session_id = config.get('session_id', 'unknown')
            region_name = config.get('region_name', 'unknown')
            target_tags = config.get('target_tags', [])
            
            # Create output directory structure using session_id
            session_dir = output_dir / str(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            
            results = []
            total_original_size = 0
            total_filtered_size = 0
            
            # Generate filter hash for logging
            filter_hash = self._generate_filter_hash(target_tags)
            
            logger.info(f"Starting filtered snapshot generation for {monthly_pbf_files.count()} monthly PBF files")
            logger.info(f"Target tags: {target_tags}")
            logger.info(f"Filter hash: {filter_hash}")
            logger.info(f"Session ID: {session_id}")
            
            for monthly_pbf in monthly_pbf_files:
                try:
                    # Step 1: Filter by tags
                    filtered_path = self._filter_by_tags(
                        monthly_pbf.path,  # Use PbfFile.path
                        target_tags,
                        session_dir
                    )
                    
                    if not filtered_path:
                        logger.warning(f"Failed to filter PBF {monthly_pbf.id}")
                        continue
                    
                    # Step 2: Create daily snapshot at month-end
                    daily_path = self._create_daily_snapshot(
                        filtered_path,
                        monthly_pbf.max_timestamp,  # Month-end timestamp
                        session_dir
                    )
                    
                    if not daily_path:
                        logger.warning(f"Failed to create daily snapshot from {filtered_path}")
                        continue
                    
                    # Clean up intermediate filtered file
                    try:
                        if filtered_path.exists():
                            filtered_path.unlink()
                            logger.debug(f"Cleaned up intermediate file: {filtered_path}")
                    except Exception as e:
                        logger.warning(f"Failed to clean up {filtered_path}: {e}")
                    
                    # Step 3: Create TemporalSnapshot record (OUTPUT)
                    # Ensure timestamp is timezone-aware
                    snapshot_timestamp = monthly_pbf.max_timestamp
                    if timezone.is_naive(snapshot_timestamp):
                        snapshot_timestamp = timezone.make_aware(snapshot_timestamp)
                    
                    # Get region from parent hierarchy with safe null checks
                    snapshot_region_name = region_name  # Default from config
                    source_pbf = None
                    
                    if monthly_pbf.parent_pbf and monthly_pbf.parent_pbf.parent_pbf:
                        source_pbf = monthly_pbf.parent_pbf.parent_pbf
                        # Derive region name from PBF path if needed
                        pbf_path = Path(source_pbf.path)
                        snapshot_region_name = pbf_path.parent.name if pbf_path.parent.name else region_name
                    
                    # Generate filter hash for uniqueness
                    filter_hash = self._generate_filter_hash(target_tags)
                    filter_config = {'target_tags': target_tags}
                    
                    # Use get_or_create with filter_hash to handle different tag sets
                    daily_snapshot, created = TemporalSnapshot.objects.get_or_create(
                        region=snapshot_region_name,
                        timestamp=snapshot_timestamp,
                        snapshot_interval='DAILY_FILTERED',
                        filter_hash=filter_hash,
                        defaults={
                            'pbf_file': source_pbf,
                            'file_path': str(daily_path),
                            'filter_config': filter_config
                        }
                    )
                    
                    # Update file_path if snapshot already existed
                    if not created:
                        daily_snapshot.file_path = str(daily_path)
                        daily_snapshot.pbf_file = source_pbf
                        daily_snapshot.filter_config = filter_config
                        daily_snapshot.save()
                        logger.info(f"Updated existing TemporalSnapshot: {daily_snapshot.id}")
                    
                    # Step 4: Generate AssetBundle if requested
                    if config.get('generate_assets', True):
                        asset_result = self.asset_service.generate_from_snapshot({
                            'snapshot_id': str(daily_snapshot.id),
                            'output_directory': str(session_dir / 'assets'),
                            'regenerate': True  # Allow regeneration for new sessions
                        })
                        
                        if not asset_result.get('success'):
                            error_msg = asset_result.get('error', 'Unknown error')
                            logger.warning(f"Failed to generate assets for {daily_snapshot.id}: {error_msg}")
                        else:
                            logger.info(f"Successfully generated assets for {daily_snapshot.id}")
                    
                    # Track sizes
                    if Path(monthly_pbf.path).exists():
                        total_original_size += Path(monthly_pbf.path).stat().st_size
                    if Path(daily_path).exists():
                        total_filtered_size += Path(daily_path).stat().st_size
                    
                    results.append({
                        'id': str(daily_snapshot.id),
                        'timestamp': daily_snapshot.timestamp.isoformat(),
                        'file_path': str(daily_path),
                        'has_asset_bundle': hasattr(daily_snapshot, 'asset_bundle')
                    })
                    
                    logger.info(f"Created filtered daily snapshot: {daily_snapshot.id}")
                    
                except Exception as e:
                    logger.error(f"Error processing monthly PBF {monthly_pbf.id}: {e}", exc_info=True)
                    continue
            
            # Calculate size reduction
            size_reduction = 0
            if total_original_size > 0:
                size_reduction = ((total_original_size - total_filtered_size) / total_original_size) * 100
            
            return {
                'success': True,
                'daily_snapshots_created': len(results),
                'total_size_mb': round(total_filtered_size / (1024 * 1024), 2),
                'original_size_mb': round(total_original_size / (1024 * 1024), 2),
                'size_reduction_percent': round(size_reduction, 2),
                'snapshots': results
            }
            
        except Exception as e:
            logger.error(f"Error in generate_filtered_daily_snapshots: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def _filter_by_tags(self, source_pbf, target_tags, output_dir):
        """
        Filter PBF by target tags using osmium tags-filter.
        
        Args:
            source_pbf: Path to source PBF file
            target_tags: List of tags (e.g., ['amenity=bubble_tea', 'cuisine=bubble_tea'])
            output_dir: Directory for output file
        
        Returns:
            Path to filtered PBF file or None if failed
        """
        try:
            source_path = Path(source_pbf)
            if not source_path.exists():
                logger.error(f"Source PBF not found: {source_pbf}")
                return None
            
            # Generate output filename
            output_filename = source_path.stem + '_filtered.osm.pbf'
            output_path = output_dir / output_filename
            
            # Build osmium tags-filter command
            cmd = [
                self.osmium_facade.OSMIUM_EXECUTABLE,
                'tags-filter',
                str(source_path)
            ]
            
            # Add target tags
            cmd.extend(target_tags)
            
            # Add output
            cmd.extend(['-o', str(output_path)])
            
            logger.info(f"Filtering tags: {' '.join(cmd)}")
            
            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )
            
            if result.returncode == 0 and output_path.exists():
                logger.info(f"Successfully filtered to {output_path}")
                return output_path
            else:
                logger.error(f"Tag filtering failed: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error(f"Tag filtering timed out for {source_pbf}")
            return None
        except Exception as e:
            logger.error(f"Error filtering tags: {e}", exc_info=True)
            return None
    
    def _create_daily_snapshot(self, filtered_pbf, timestamp, output_dir):
        """
        Create daily snapshot at specific timestamp using osmium time-filter.
        
        Args:
            filtered_pbf: Path to filtered PBF file
            timestamp: DateTime for snapshot
            output_dir: Directory for output file
        
        Returns:
            Path to daily snapshot PBF file or None if failed
        """
        try:
            filtered_path = Path(filtered_pbf)
            if not filtered_path.exists():
                logger.error(f"Filtered PBF not found: {filtered_pbf}")
                return None
            
            # Generate output filename with date
            date_str = timestamp.strftime('%Y_%m_%d')
            # Remove .osm from stem and create proper filename
            base_name = filtered_path.stem.replace('.osm_filtered', '').replace('_filtered', '')
            output_filename = f"{base_name}_daily_{date_str}.osm.pbf"
            output_path = output_dir / output_filename
            
            # Build osmium time-filter command
            # Osmium requires Z suffix, not +00:00
            timestamp_iso = timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')
            
            cmd = [
                self.osmium_facade.OSMIUM_EXECUTABLE,
                'time-filter',
                str(filtered_path),
                timestamp_iso,
                '-o', str(output_path),
                '--overwrite'
            ]
            
            logger.info(f"Creating daily snapshot: {' '.join(cmd)}")
            
            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0 and output_path.exists():
                logger.info(f"Successfully created daily snapshot: {output_path}")
                
                # Clean up filtered intermediate file
                try:
                    filtered_path.unlink()
                    logger.info(f"Cleaned up intermediate file: {filtered_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {filtered_path}: {e}")
                
                return output_path
            else:
                logger.error(f"Time filtering failed: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error(f"Time filtering timed out for {filtered_pbf}")
            return None
        except Exception as e:
            logger.error(f"Error creating daily snapshot: {e}", exc_info=True)
            return None
