"""
Daily Snapshot Service

Generates generic daily snapshots from monthly PBF files WITHOUT tag filtering.
Tag filtering is deferred to asset generation phase for maximum flexibility.

This service:
1. Takes monthly PBF files as input
2. Generates daily snapshots at month-end using osmium time-filter
3. Creates TemporalSnapshot records with no filter_config
4. Supports parallel processing using E-core CPU pinning

Author: EDA Vector Search Toolkit
"""
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from api.models import TemporalSnapshot, PbfFile
from extraction.services.osmium_facade import OsmiumFacade
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

logger = logging.getLogger(__name__)


class DailySnapshotService:
    """
    Service for generating generic daily snapshots from monthly PBF files.
    
    Workflow:
    1. Take monthly PBF files as source
    2. Generate daily snapshot at month-end (osmium time-filter)
    3. Create TemporalSnapshot record (no filter_config)
    4. Asset generation will apply tag filtering later
    """
    
    def __init__(self):
        self.osmium_facade = OsmiumFacade()
        self.osmium_executable = getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium')
    
    def generate_daily_snapshots(self, config):
        """
        Generate generic daily snapshots from monthly PBF files.
        
        Args:
            config = {
                'monthly_pbf_ids': [uuid1, uuid2, ...],  # PbfFile IDs
                'output_directory': 'backend/data/daily_snapshots',
                'region_name': 'ukraine',  # For metadata
                'parallel': True,  # Use parallel processing
                'max_workers': 4,  # Number of parallel workers
                'use_e_cores': True,  # Pin to E-cores if available
            }
        
        Returns:
            {
                'success': True,
                'daily_snapshots_created': 72,
                'total_size_mb': 1800,
                'snapshots': [...]
            }
        """
        try:
            monthly_pbf_files = PbfFile.objects.filter(
                id__in=config['monthly_pbf_ids']
            ).order_by('min_timestamp')
            
            if not monthly_pbf_files.exists():
                return {
                    'success': False,
                    'error': 'No monthly PBF files found'
                }
            
            output_dir = Path(config.get('output_directory', 
                                        settings.DAILY_SNAPSHOTS_DIR if hasattr(settings, 'DAILY_SNAPSHOTS_DIR') 
                                        else 'backend/data/daily_snapshots'))
            output_dir.mkdir(parents=True, exist_ok=True)
            
            region_name = config.get('region_name', 'unknown')
            parallel = config.get('parallel', False)
            max_workers = config.get('max_workers', 4)
            use_e_cores = config.get('use_e_cores', True)
            
            logger.info(f"Starting daily snapshot generation for {monthly_pbf_files.count()} monthly PBF files")
            logger.info(f"Region: {region_name}")
            logger.info(f"Parallel processing: {parallel} (workers: {max_workers})")
            logger.info(f"Use E-cores: {use_e_cores}")
            
            results = []
            total_size = 0
            
            if parallel:
                # Parallel processing with E-core pinning
                results, total_size = self._process_parallel(
                    monthly_pbf_files, 
                    output_dir, 
                    region_name,
                    max_workers,
                    use_e_cores
                )
            else:
                # Sequential processing
                for monthly_pbf in monthly_pbf_files:
                    result = self._process_single_snapshot(
                        monthly_pbf, 
                        output_dir, 
                        region_name,
                        None  # No core pinning for sequential
                    )
                    if result:
                        results.append(result)
                        if Path(result['file_path']).exists():
                            total_size += Path(result['file_path']).stat().st_size
            
            return {
                'success': True,
                'daily_snapshots_created': len(results),
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'snapshots': results
            }
            
        except Exception as e:
            logger.error(f"Error in generate_daily_snapshots: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def _process_parallel(self, monthly_pbf_files, output_dir, region_name, max_workers, use_e_cores):
        """Process snapshots in parallel with optional E-core pinning."""
        results = []
        total_size = 0
        
        # Get E-cores if requested
        e_cores = []
        if use_e_cores:
            e_cores = self._get_e_core_list()
            if e_cores:
                logger.info(f"Using E-cores: {e_cores}")
            else:
                logger.warning("E-cores not available, using default scheduling")
        
        used_cores = set()
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            
            for monthly_pbf in monthly_pbf_files:
                # Get next available E-core
                core = None
                if e_cores:
                    core = self._get_next_available_e_core(e_cores, used_cores)
                    if core is not None:
                        used_cores.add(core)
                
                future = executor.submit(
                    self._process_single_snapshot,
                    monthly_pbf,
                    output_dir,
                    region_name,
                    core
                )
                futures[future] = (monthly_pbf, core)
            
            for future in as_completed(futures):
                monthly_pbf, core = futures[future]
                
                # Release core
                if core is not None and core in used_cores:
                    used_cores.remove(core)
                
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        if Path(result['file_path']).exists():
                            total_size += Path(result['file_path']).stat().st_size
                except Exception as e:
                    logger.error(f"Error processing {monthly_pbf.id}: {e}", exc_info=True)
        
        return results, total_size
    
    def _process_single_snapshot(self, monthly_pbf, output_dir, region_name, core=None):
        """
        Process a single monthly PBF to create a daily snapshot.
        
        Args:
            monthly_pbf: PbfFile instance
            output_dir: Output directory path
            region_name: Region name for metadata
            core: CPU core to pin to (optional)
        
        Returns:
            Dict with snapshot info or None if failed
        """
        try:
            # Create daily snapshot at month-end
            daily_path = self._create_daily_snapshot(
                monthly_pbf.path,
                monthly_pbf.max_timestamp,
                output_dir,
                core
            )
            
            if not daily_path:
                logger.warning(f"Failed to create daily snapshot from {monthly_pbf.id}")
                return None
            
            # Ensure timestamp is timezone-aware
            snapshot_timestamp = monthly_pbf.max_timestamp
            if timezone.is_naive(snapshot_timestamp):
                snapshot_timestamp = timezone.make_aware(snapshot_timestamp)
            
            # Get source PBF (region extract)
            source_pbf = None
            snapshot_region_name = region_name
            
            if monthly_pbf.parent_pbf and monthly_pbf.parent_pbf.parent_pbf:
                source_pbf = monthly_pbf.parent_pbf.parent_pbf
                pbf_path = Path(source_pbf.path)
                snapshot_region_name = pbf_path.parent.name if pbf_path.parent.name else region_name
            
            # Create TemporalSnapshot record WITHOUT filter_config
            # Use 'DAILY' interval (not 'DAILY_FILTERED')
            daily_snapshot, created = TemporalSnapshot.objects.get_or_create(
                region=snapshot_region_name,
                timestamp=snapshot_timestamp,
                snapshot_interval='DAILY',
                filter_hash='',  # Empty for generic snapshots
                defaults={
                    'pbf_file': source_pbf,
                    'file_path': str(daily_path),
                    'filter_config': None  # No filtering applied
                }
            )
            
            # Update file_path if snapshot already existed
            if not created:
                daily_snapshot.file_path = str(daily_path)
                daily_snapshot.pbf_file = source_pbf
                daily_snapshot.filter_config = None
                daily_snapshot.save()
                logger.info(f"Updated existing TemporalSnapshot: {daily_snapshot.id}")
            else:
                logger.info(f"Created new TemporalSnapshot: {daily_snapshot.id}")
            
            return {
                'id': str(daily_snapshot.id),
                'timestamp': daily_snapshot.timestamp.isoformat(),
                'file_path': str(daily_path),
                'region': snapshot_region_name
            }
            
        except Exception as e:
            logger.error(f"Error processing monthly PBF {monthly_pbf.id}: {e}", exc_info=True)
            return None
    
    def _create_daily_snapshot(self, source_pbf, timestamp, output_dir, core=None):
        """
        Create daily snapshot at specific timestamp using osmium time-filter.
        
        Args:
            source_pbf: Path to source PBF file
            timestamp: DateTime for snapshot
            output_dir: Directory for output file
            core: CPU core to pin to (optional)
        
        Returns:
            Path to daily snapshot PBF file or None if failed
        """
        try:
            source_path = Path(source_pbf)
            if not source_path.exists():
                logger.error(f"Source PBF not found: {source_pbf}")
                return None
            
            # Generate output filename with date
            date_str = timestamp.strftime('%Y_%m_%d')
            base_name = source_path.stem.replace('.osm', '')
            output_filename = f"{base_name}_daily_{date_str}.osm.pbf"
            output_path = output_dir / output_filename
            
            # Build osmium time-filter command
            timestamp_iso = timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')
            
            cmd = [
                self.osmium_executable,
                'time-filter',
                str(source_path),
                timestamp_iso,
                '-o', str(output_path),
                '--overwrite'
            ]
            
            # Add CPU core pinning if specified
            if core is not None:
                cmd = ['taskset', '-c', str(core)] + cmd
                logger.info(f"Creating daily snapshot on core {core}: {output_filename}")
            else:
                logger.info(f"Creating daily snapshot: {output_filename}")
            
            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )
            
            if result.returncode == 0 and output_path.exists():
                file_size_mb = output_path.stat().st_size / (1024 * 1024)
                logger.info(f"Successfully created daily snapshot: {output_path} ({file_size_mb:.2f} MB)")
                return output_path
            else:
                logger.error(f"Time filtering failed: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error(f"Time filtering timed out for {source_pbf}")
            return None
        except Exception as e:
            logger.error(f"Error creating daily snapshot: {e}", exc_info=True)
            return None
    
    def _get_e_core_list(self):
        """
        Identify E-cores (efficiency cores) on the system.
        Based on extraction_service.py implementation.
        
        Returns:
            List of E-core CPU IDs or empty list if not available
        """
        try:
            # Check if lscpu is available
            result = subprocess.run(
                ['lscpu', '-p=CPU,MAXMHZ'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return []
            
            # Parse output to find cores with lower max frequency
            cores_freq = []
            for line in result.stdout.split('\n'):
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.split(',')
                if len(parts) >= 2:
                    try:
                        cpu_id = int(parts[0])
                        max_freq = float(parts[1])
                        cores_freq.append((cpu_id, max_freq))
                    except ValueError:
                        continue
            
            if not cores_freq:
                return []
            
            # Sort by frequency
            cores_freq.sort(key=lambda x: x[1])
            
            # E-cores typically have lower max frequency
            # For i7-14700K: E-cores are typically cores 12-19
            # Identify cores with significantly lower frequency
            if len(cores_freq) > 8:
                freq_threshold = cores_freq[len(cores_freq) // 2][1]
                e_cores = [cpu_id for cpu_id, freq in cores_freq if freq < freq_threshold]
                
                if e_cores:
                    logger.info(f"Detected E-cores: {e_cores}")
                    return e_cores
            
            return []
            
        except Exception as e:
            logger.warning(f"Could not detect E-cores: {e}")
            return []
    
    def _get_next_available_e_core(self, e_cores, used_cores):
        """
        Get next available E-core that's not currently in use.
        
        Args:
            e_cores: List of E-core IDs
            used_cores: Set of currently used core IDs
        
        Returns:
            Core ID or None if all cores are in use
        """
        for core in e_cores:
            if core not in used_cores:
                return core
        return None
