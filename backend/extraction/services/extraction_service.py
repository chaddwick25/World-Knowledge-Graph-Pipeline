import json
import logging
import os
import psutil
import random
import subprocess
import time
import urllib.request
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from django.conf import settings
from django.utils import timezone

from .pbf_cache_manager import PBFCacheManager, MultiJobCacheCoordinator
from .pbf_hierarchy_resolver import pbf_hierarchy_resolver
from .osmium_facade import OsmiumFacade

logger = logging.getLogger(__name__)

def get_e_core_list() -> List[int]:
    """
    Get list of E-cores (efficiency cores) on Intel i7-14700K.
    E-cores are typically cores 12-19 on hybrid architecture.
    """
    total_cores = psutil.cpu_count(logical=True)
    if total_cores >= 20:  # i7-14700K has 20 logical cores
        # E-cores are typically the last 8 cores (12-19)
        return list(range(12, 20))
    else:
        # Fallback for other systems - use last quarter of cores
        e_core_start = total_cores * 3 // 4
        return list(range(e_core_start, total_cores))

def get_next_available_e_core(used_cores: set) -> Optional[int]:
    """
    Get the next available E-core that's not currently in use.
    """
    e_cores = get_e_core_list()
    for core in e_cores:
        if core not in used_cores:
            return core
    return None

class ExtractionService:
    """
    High-level service for PBF extraction operations.
    Maintains state for core allocation and provides a simple API for common tasks.
    """
    def __init__(self):
        self.used_cores = set()
        self.osmium = OsmiumFacade()

    def extract_with_polygon(self, source_pbf_path: str, polygon_file_path: str, 
                             output_path: str, source_pbf_file = None,
                             job_id: Optional[str] = None) -> dict:
        """
        Extract a geographical area using a .poly file boundary.
        Uses intelligent caching and E-core optimization.
        """
        result = run_pbf_extraction_with_caching(
            source_pbf_path=source_pbf_path,
            poly_file_path=polygon_file_path,
            output_pbf_path=output_path,
            used_cores=self.used_cores,
            job_id=job_id
        )
        
        # If successful, register in database
        if result.get('status') == 'completed':
            try:
                # Find source_pbf if not provided
                if not source_pbf_file:
                    from api.models import PbfFile
                    source_pbf_file = PbfFile.objects.filter(path=source_pbf_path).first()
                
                register_extraction_result(
                    poly_file_path=polygon_file_path,
                    output_pbf_path=output_path,
                    source_pbf=source_pbf_file,
                    metrics=result
                )
                result['db_registered'] = True
            except Exception as e:
                print(f"Warning: Database registration failed: {e}")
                result['db_registered'] = False
        
        return result

    def create_snapshot(self, source_pbf_path: str, output_path: str, timestamp: str, extraction_level: str = None) -> dict:
        """
        Creates a point-in-time snapshot (no history) from a historical PBF.
        Preserves metadata (version, user, changeset).
        """
        try:
            start_time = timezone.now()
            
            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Using osmium time-filter (single timestamp flattens history)
            result = self.osmium.time_filter(
                input_file=source_pbf_path,
                timestamp=timestamp,
                output_file=output_path
            )
            
            if result.get('success'):
                from api.models import PbfFile
                if extraction_level is None:
                    extraction_level = PbfFile.ExtractionLevel.SNAPSHOT
                
                end_time = timezone.now()
                result['duration_seconds'] = (end_time - start_time).total_seconds()
                result['output_file_size_bytes'] = os.path.getsize(output_path)
                result['status'] = 'completed'
                
                # Database registration
                result['extraction_level'] = extraction_level
                logger.info(f"DB Lookup: Searching for PBF with path: '{source_pbf_path}'")
                source_pbf = PbfFile.objects.filter(path=source_pbf_path).first()
                if not source_pbf:
                    logger.warning(f"DB Lookup FAILED for '{source_pbf_path}'. Total PBFs in DB: {PbfFile.objects.count()}")
                
                # Fallback: if source exists on disk but not in DB, register it as a region
                if not source_pbf and os.path.exists(source_pbf_path):
                    source_pbf = PbfFile.objects.create(
                        path=source_pbf_path,
                        pbf_file_type=PbfFile.PbfType.REGION,
                        status=PbfFile.PbfStatus.COMPLETED,
                        has_history=True,
                        size_bytes=os.path.getsize(source_pbf_path)
                    )

                register_extraction_result(
                    poly_file_path="",  # Path-based snapshot filter
                    output_pbf_path=output_path,
                    source_pbf=source_pbf,
                    metrics=result
                )
            
            return result
        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'failed'}

    def time_slice(self, source_pbf_path: str, output_path: str, start_time: str, end_time: str, extraction_level: str = None) -> dict:
        """
        Create a temporal slice (preserving history within the range).
        """
        try:
            start_timer = timezone.now()
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            result = self.osmium.time_filter(
                input_file=source_pbf_path,
                timestamp=start_time,
                output_file=output_path,
                end_timestamp=end_time
            )
            
            if result.get('success'):
                finish_timer = timezone.now()
                result['duration_seconds'] = (finish_timer - start_timer).total_seconds()
                result['output_file_size_bytes'] = os.path.getsize(output_path)
                result['status'] = 'completed'
                
                result['extraction_level'] = extraction_level
                
                source_pbf = PbfFile.objects.filter(path=source_pbf_path).first()
                register_extraction_result(
                    poly_file_path="",
                    output_pbf_path=output_path,
                    source_pbf=source_pbf,
                    metrics=result
                )
            return result
        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'failed'}

def register_extraction_result(poly_file_path: str, output_pbf_path: str, source_pbf, metrics: dict):
    """
    Registers a successful PBF extraction in the database.
    Updates PbfExtract, PbfFile, and RegionHierarchy models.
    """
    try:
        output_file_size = metrics.get('output_file_size_bytes', 0)
        start_time = metrics.get('start_time', timezone.now())
        end_time = metrics.get('end_time', timezone.now())
        duration_seconds = metrics.get('duration_seconds', 0)
        cpu_core_id = metrics.get('cpu_core_id', 0)
        
        # 1. Update/Create PbfExtract record
        from api.models import PbfExtract, PbfFile, RegionHierarchy
        if not source_pbf:
            raise RuntimeError(f"Cannot register extraction result for {output_pbf_path}: source_pbf is None. Ensure source is registered in PbfFile table.")
            
        PbfExtract.objects.update_or_create(
            output_pbf_path=output_pbf_path,
            defaults={
                'source_pbf': source_pbf,
                'poly_file_path': poly_file_path,
                'source_file_size_bytes': metrics.get('source_file_size_bytes', 0),
                'start_time': start_time,
                'end_time': end_time,
                'duration_seconds': duration_seconds,
                'output_file_size_bytes': output_file_size,
                'osmium_extract_params': metrics.get('osmium_params', {}),
                'cpu_core_id': cpu_core_id,
            }
        )

        # 2. Register/Update PbfFile record
        pbf_type = PbfFile.PbfType.REGION
        if 'continents' in poly_file_path or 'continent' in output_pbf_path.lower():
            pbf_type = PbfFile.PbfType.CONTINENT
            
        timestamp_info = {'min_timestamp': None, 'max_timestamp': None, 'method': 'UNKNOWN'}
        
        # Regex patterns for filenames: 2021_01.pbf, snapshot_2021_01_31.pbf, or 2021.pbf
        import re
        yearly_match = re.search(r'(\d{4})\.pbf$', output_pbf_path)
        monthly_match = re.search(r'(\d{4})_(\d{2})\.pbf$', output_pbf_path)
        snapshot_match = re.search(r'snapshot_(\d{4})_(\d{2})_(\d{2})', output_pbf_path)
        
        if snapshot_match:
            y, m, d = map(int, snapshot_match.groups())
            timestamp_info = {
                'min_timestamp': datetime(2004, 1, 1, tzinfo=timezone.utc),
                'max_timestamp': datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc),
                'method': PbfFile.TemporalMetadataSource.AUTO_DETECTED
            }
        elif monthly_match:
            y, m = map(int, monthly_match.groups())
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            timestamp_info = {
                'min_timestamp': datetime(y, m, 1, 0, 0, 0, tzinfo=timezone.utc),
                'max_timestamp': datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc),
                'method': PbfFile.TemporalMetadataSource.AUTO_DETECTED
            }
        elif yearly_match:
            y = int(yearly_match.group(1))
            timestamp_info = {
                'min_timestamp': datetime(y, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                'max_timestamp': datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
                'method': PbfFile.TemporalMetadataSource.AUTO_DETECTED
            }
        elif source_pbf and source_pbf.min_timestamp and source_pbf.max_timestamp:
            timestamp_info = {
                'min_timestamp': source_pbf.min_timestamp,
                'max_timestamp': source_pbf.max_timestamp,
                'method': PbfFile.TemporalMetadataSource.INHERITED
            }

        new_pbf_file, created = PbfFile.objects.update_or_create(
            path=output_pbf_path,
            defaults={
                'size_bytes': output_file_size,
                'pbf_file_type': pbf_type,
                'status': PbfFile.PbfStatus.COMPLETED,
                'has_history': True,
                'source_url': source_pbf.source_url if source_pbf else "",
                'parent_pbf': source_pbf,
                'min_timestamp': timestamp_info['min_timestamp'],
                'max_timestamp': timestamp_info['max_timestamp'],
                'temporal_metadata_source': timestamp_info['method'],
                'extraction_level': metrics.get('extraction_level', PbfFile.ExtractionLevel.REGION),
                'yearly_extracts_generated': False,
                'yearly_extracts_count': 0,
                'monthly_extracts_generated': False,
                'monthly_extracts_count': 0
            }
        )

        # 3. Link to RegionHierarchy (Skip for temporal slices/empty poly paths)
        if poly_file_path:
            region_node = RegionHierarchy.objects.filter(poly_file_path__icontains=Path(poly_file_path).name).first()
            if region_node:
                region_node.corresponding_pbf = new_pbf_file
                region_node.save()
            
        return True
    except Exception as e:
        print(f"Error in register_extraction_result: {e}")
        return False

def run_pbf_extraction(source_pbf_id: str, poly_file_path: str, output_pbf_path: str, cpu_core_id: int, task_id: str):
    """
    Runs the osmium extract process for a given PBF file and polygon, pinning it to a specific CPU core.
    Tracks the entire process in the PbfExtract model.
    """
    try:
        planet_pbf = PbfFile.objects.get(id=source_pbf_id)
        task = Task.objects.get(id=task_id)
    except (PbfFile.DoesNotExist, Task.DoesNotExist) as e:
        print(f"Error: Could not find source PBF or Task. {e}")
        return

    # Find the optimal source PBF using hierarchy resolution
    try:
        optimal_pbf, selection_reason = pbf_hierarchy_resolver.find_optimal_source_pbf(
            poly_file_path=poly_file_path,
            fallback_planet_pbf=planet_pbf
        )
        print(f"Source selection: {selection_reason}")
        source_pbf = optimal_pbf
    except Exception as e:
        print(f"Warning: Hierarchy resolution failed ({e}), using original source")
        source_pbf = planet_pbf

    start_time = timezone.now()
    source_file_size = os.path.getsize(source_pbf.path)

    # Use taskset --cpu-list 2-27 (High-performance cores)
    cpu_cores = '2-27' 
    osmium_path = settings.OSMIUM_BINARY_PATH
    command = [
        'taskset',
        '--cpu-list',
        cpu_cores,
        osmium_path,
        'extract',
        '--progress',
        '--with-history',
        '--strategy=complete_ways',
        '--overwrite',
        '-p', poly_file_path,
        source_pbf.path,
        '-o', output_pbf_path
    ]

    try:
        print(f"Running command: {' '.join(command)}")
        subprocess.run(command, check=True)
        
        end_time = timezone.now()
        output_file_size = os.path.getsize(output_pbf_path)
        
        metrics = {
            'status': 'completed',
            'output_file_size_bytes': output_file_size,
            'source_file_size_bytes': source_file_size,
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': (end_time - start_time).total_seconds(),
            'cpu_core_id': 12, # representative E-core
            'osmium_params': {'with-history': True, 'strategy': 'complete_ways'}
        }
        
        register_extraction_result(poly_file_path, output_pbf_path, source_pbf, metrics)

        task.status = Task.TaskStatus.COMPLETED
        task.result = {'output_path': output_pbf_path, 'size': output_file_size}
        task.save()

        print(f"Extraction completed successfully for {output_pbf_path}")

    except Exception as e:
        task.status = Task.TaskStatus.FAILED
        task.result = {'error': str(e)}
        task.save()
        print(f"Error during extraction: {e}")
        raise

# Global cache manager instance
_cache_manager = None
_cache_coordinator = None

def get_cache_manager() -> PBFCacheManager:
    """Get or create global cache manager instance."""
    global _cache_manager
    if _cache_manager is None:
        from django.conf import settings
        cache_dir = getattr(settings, 'PBF_CACHE_DIR', str(Path(settings.BASE_DATA_DIR) / 'pbf_cache'))
        _cache_manager = PBFCacheManager(cache_dir, max_ram_cache_gb=96, max_disk_cache_gb=1200)
    return _cache_manager

def get_cache_coordinator() -> MultiJobCacheCoordinator:
    """Get or create global cache coordinator instance."""
    global _cache_coordinator
    if _cache_coordinator is None:
        _cache_coordinator = MultiJobCacheCoordinator(get_cache_manager())
    return _cache_coordinator

def run_pbf_extraction_with_e_cores(source_pbf_path: str, poly_file_path: str, output_pbf_path: str, used_cores: set) -> dict:
    """
    Enhanced extraction function that uses E-cores and returns processing metrics.
    """
    return run_pbf_extraction_with_caching(source_pbf_path, poly_file_path, output_pbf_path, used_cores)

# Thread-safe cache for relation bboxes to avoid redundant scans
_bbox_cache = {}
_bbox_lock = threading.Lock()

def _get_relation_bbox_locally(source_pbf_path: str, relation_id: int, cpu_core_id: int) -> dict:
    """
    Fetch the bounding box for an OSM relation LOCALLY from the source PBF.
    Uses 'osmium getid --add-referenced' to isolate the relation + its nodes.
    """
    cache_key = f"{source_pbf_path}_{relation_id}"
    
    # Fast path: already in cache
    with _bbox_lock:
        if cache_key in _bbox_cache:
            return _bbox_cache[cache_key]

    # Slow path: only one thread scans at a time for the same relation
    # Using a global lock for simplicity since we usually process one region at a time
    with _bbox_lock:
        # Re-check inside lock
        if cache_key in _bbox_cache:
            return _bbox_cache[cache_key]

        import tempfile
        
        # We use a temp file to hold the relation + its members
        with tempfile.NamedTemporaryFile(suffix='.pbf', delete=False) as tmp:
            tmp_rel_pbf = tmp.name
    
        try:
            # Step A: Extract the relation AND its members (nodes/ways) to get coordinates
            # We need --add-referenced to include the nodes which have the actual locations.
            getid_cmd = [
                settings.OSMIUM_BINARY_PATH, 'getid',
                '--with-history',
                '--add-referenced',
                source_pbf_path, f"r{relation_id}",
                '-o', tmp_rel_pbf,
                '--overwrite'
            ]
            
            logger.info(f"[Core {cpu_core_id}] Scanning {Path(source_pbf_path).name} for r{relation_id} bounds...")
            try:
                subprocess.run(getid_cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"[Core {cpu_core_id}] Osmium getid FAILED: {e.stderr}")
                raise
            
            # Step B: Use fileinfo to get the data bbox from the temp file
            # Note: We use '-e' (extended) and '-j' (json output)
            logger.info(f"[Core {cpu_core_id}] Running EXTENDED fileinfo on {tmp_rel_pbf}")
            info_cmd = [
                settings.OSMIUM_BINARY_PATH, 'fileinfo',
                '-e',
                '-j',
                tmp_rel_pbf
            ]
            res = subprocess.run(info_cmd, check=True, capture_output=True, text=True)
            info_data = json.loads(res.stdout)
            
            # Osmium returns [min_lon, min_lat, max_lon, max_lat]
            bbox_list = info_data.get('data', {}).get('bbox', [])
            if not bbox_list:
                raise RuntimeError(f"Could not find geographic bounds for r{relation_id} (is it missing nodes in {source_pbf_path}?)")
                
            bbox = {
                "left":   bbox_list[0],
                "bottom": bbox_list[1],
                "right":  bbox_list[2],
                "top":    bbox_list[3],
            }
            
            _bbox_cache[cache_key] = bbox
            return bbox
            
        finally:
            if os.path.exists(tmp_rel_pbf):
                os.unlink(tmp_rel_pbf)


def run_pbf_extraction_with_relation(source_pbf_path: str, relation_id: int, output_pbf_path: str, used_cores: set, job_id: str = None) -> dict:
    """
    Extracts a geographical area using an OSM Relation ID.
    1. Resolves the relation's bounding box LOCALLY from the source PBF.
    2. Passes that bbox as the extraction geometry to osmium extract.
    """
    cpu_core_id = get_next_available_e_core(used_cores)
    if cpu_core_id is None:
        cpu_core_id = 12  # safe fallback

    used_cores.add(cpu_core_id)

    if job_id is None:
        job_id = f"relation_{relation_id}_{cpu_core_id}"

    import tempfile

    try:
        # Step 1: Resolve bbox LOCALLY
        bbox = _get_relation_bbox_locally(source_pbf_path, relation_id, cpu_core_id)
        logger.info(f"[Core {cpu_core_id}] Local Bbox resolved: {bbox}")

        # Step 2: Build osmium extract config with bbox geometry
        config = {
            "extracts": [
                {
                    "output": output_pbf_path,
                    "output_format": "pbf",
                    "bbox": bbox
                }
            ],
            "strategy": "complete_ways"
        }

        config_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(config, config_file)
        config_file.close()

        start_time = timezone.now()
        os.makedirs(os.path.dirname(output_pbf_path), exist_ok=True)

        osmium_path = settings.OSMIUM_BINARY_PATH
        command = [
            'taskset', '--cpu-list', '2-27',
            osmium_path, 'extract',
            '--progress',
            '--config', config_file.name,
            '--with-history',
            '--overwrite',
            source_pbf_path
        ]

        logger.info(f"[Core {cpu_core_id}] Running: {' '.join(command)}")
        # Let stderr pass through so osmium progress bar is visible in terminal
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None)
        stdout, _ = process.communicate()

        end_time = timezone.now()
        duration_seconds = (end_time - start_time).total_seconds()
        output_file_size = os.path.getsize(output_pbf_path) if process.returncode == 0 and os.path.exists(output_pbf_path) else 0

        if process.returncode != 0:
            raise RuntimeError(f"Osmium failed with exit code {process.returncode}")

        logger.info(f"[Core {cpu_core_id}] Done: {output_pbf_path} ({output_file_size / 1024 / 1024:.1f} MB in {duration_seconds:.1f}s)")

        result = {
            'status': 'completed',
            'success': True,
            'output_pbf_path': output_pbf_path,
            'duration_seconds': duration_seconds,
            'output_file_size_bytes': output_file_size,
            'cpu_core_id': cpu_core_id,
            'job_id': job_id,
            'start_time': start_time,
            'end_time': end_time,
            'osmium_params': {'bbox': bbox, 'strategy': 'complete_ways'}
        }

        # Database Registration
        from api.models import PbfFile
        logger.info(f"DB Lookup (Phase 1): Searching for path: '{source_pbf_path}'")
        source_pbf = PbfFile.objects.filter(path=source_pbf_path).first()
        if not source_pbf:
             logger.warning(f"DB Lookup (Phase 1) FAILED for '{source_pbf_path}'. Total PBFs in DB: {PbfFile.objects.count()}")
        
        # Fallback: if source exists on disk but not in DB, register it
        if not source_pbf and os.path.exists(source_pbf_path):
            source_pbf = PbfFile.objects.create(
                path=source_pbf_path,
                pbf_file_type=PbfFile.PbfType.REGION,
                status=PbfFile.PbfStatus.COMPLETED,
                has_history=True,
                size_bytes=os.path.getsize(source_pbf_path)
            )

        register_extraction_result(
            poly_file_path="",  # Relation-based extraction
            output_pbf_path=output_pbf_path,
            source_pbf=source_pbf,
            metrics=result
        )

        return result

    except Exception as e:
        logger.error(f"[Core {cpu_core_id}] Error during relation extraction: {e}")
        return {'status': 'failed', 'success': False, 'error': str(e)}
    finally:
        used_cores.discard(cpu_core_id)
        if 'config_file' in locals() and os.path.exists(config_file.name):
            try:
                os.unlink(config_file.name)
            except:
                pass


def run_pbf_extraction_with_caching(source_pbf_path: str, poly_file_path: str, output_pbf_path: str, used_cores: set, job_id: str = None) -> dict:
    """
    Enhanced extraction function with intelligent caching and E-core allocation.
    
    Now includes hierarchy resolution to automatically select the closest parent PBF
    instead of always using the planet file.
    """
    # Get available E-core
    cpu_core_id = get_next_available_e_core(used_cores)
    if cpu_core_id is None:
        raise RuntimeError("No available E-cores for processing")
    
    used_cores.add(cpu_core_id)
    
    # Initialize caching
    cache_manager = get_cache_manager()
    cache_coordinator = get_cache_coordinator()
    
    if job_id is None:
        job_id = f"extract_{Path(poly_file_path).stem}_{cpu_core_id}"
    
    try:
        start_time = timezone.now()
        
        # Use hierarchy resolution to find optimal source
        try:
            from api.models import PbfFile
            # Get the planet PBF as fallback
            planet_pbf = PbfFile.objects.filter(
                pbf_file_type=PbfFile.PbfType.PLANET,
                has_history=True,
                status=PbfFile.PbfStatus.COMPLETED
            ).first()
            
            if planet_pbf:
                optimal_pbf, selection_reason = pbf_hierarchy_resolver.find_optimal_source_pbf(
                    poly_file_path=poly_file_path,
                    fallback_planet_pbf=planet_pbf
                )
                print(f"[Core {cpu_core_id}] {selection_reason}")
                
                # Use the optimal PBF path
                source_pbf_path = optimal_pbf.path
                
                if optimal_pbf.id != planet_pbf.id:
                    speedup = pbf_hierarchy_resolver.estimate_extraction_speedup(optimal_pbf, planet_pbf)
                    print(f"[Core {cpu_core_id}] Estimated speedup: {speedup:.1f}x")
        except Exception as e:
            print(f"[Core {cpu_core_id}] Hierarchy resolution warning: {e}")
        
        # Check for cached source or better alternative
        best_source = cache_manager.get_best_cached_source(poly_file_path, source_pbf_path)
        
        # Acquire read access to the source file
        actual_source, memory_map = cache_coordinator.concurrent_access.acquire_read_access(best_source, job_id)
        
        source_file_size = os.path.getsize(actual_source)
        
        # Create output directory if it doesn't exist
        os.makedirs(os.path.dirname(output_pbf_path), exist_ok=True)
        
        print(f"[Core {cpu_core_id}] Using source: {Path(actual_source).name} (cached: {actual_source != source_pbf_path})")
        if memory_map:
            print(f"[Core {cpu_core_id}] Memory-mapped source: {len(memory_map) / 1024**3:.2f}GB")
        
        extract_params = {
            'with-history': True,
            'strategy': 'complete_ways'
        }
        
        # Using user-requested high-performance range 2-27 for ALL tasks
        cpu_cores_str = "2-27"
        print(f"[Multi-Core] Utilizing core range: {cpu_cores_str}")

        # Construct the command with correct osmium path
        osmium_path = settings.OSMIUM_BINARY_PATH
        command = [
            'taskset',
            '--cpu-list',
            cpu_cores_str,
            osmium_path,
            'extract',
            '--progress',
            '--with-history',
            '--strategy=complete_ways',
            '-p', poly_file_path,
            actual_source,
            '-o', output_pbf_path
        ]
        
        print(f"[Core {cpu_core_id}] Running: {' '.join(command)}")
        # Inherit stderr to allow the progress bar to show in the terminal
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None)
        stdout, _ = process.communicate()
        
        # Record timing and size immediately
        end_time = timezone.now()
        duration_seconds = (end_time - start_time).total_seconds()
        output_file_size = os.path.getsize(output_pbf_path) if process.returncode == 0 and os.path.exists(output_pbf_path) else 0

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr)
        
        metrics = {
                'source_path': source_pbf_path,
                'actual_source_path': actual_source,
                'poly_path': poly_file_path,
                'output_path': output_pbf_path,
                'cpu_core_id': cpu_core_id,
                'job_id': job_id,
                'start_time': start_time,
                'end_time': end_time,
                'duration_seconds': duration_seconds,
                'source_file_size_bytes': source_file_size,
                'output_file_size_bytes': output_file_size,
                'reduction_ratio': (1 - output_file_size / source_file_size) if source_file_size > 0 else 0,
            'source_path': source_pbf_path,
            'actual_source_path': actual_source,
            'poly_path': poly_file_path,
            'output_path': output_pbf_path,
            'cpu_core_id': cpu_core_id,
            'job_id': job_id,
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': duration_seconds,
            'source_file_size_bytes': source_file_size,
            'output_file_size_bytes': output_file_size,
            'reduction_ratio': (1 - output_file_size / source_file_size) if source_file_size > 0 else 0,
            'processing_rate_mb_per_sec': (source_file_size / (1024 * 1024)) / duration_seconds if duration_seconds > 0 else 0,
            'cache_hit': actual_source != source_pbf_path,
            'memory_mapped': memory_map is not None,
            'status': 'completed',
            'success': True,
            'osmium_params': extract_params
        }
        
        print(f"[Core {cpu_core_id}] Extraction completed: {output_pbf_path}")
        print(f"[Core {cpu_core_id}] Duration: {duration_seconds:.2f}s, Rate: {metrics['processing_rate_mb_per_sec']:.2f} MB/s")
        print(f"[Core {cpu_core_id}] Cache hit: {metrics['cache_hit']}, Memory mapped: {metrics['memory_mapped']}")
        
        # Release read access
        cache_coordinator.concurrent_access.release_read_access(best_source, job_id)
        
        return metrics
        
    except Exception as e:
        end_time = timezone.now()
        duration_seconds = (end_time - start_time).total_seconds()
        
        metrics = {
            'source_path': source_pbf_path,
            'actual_source_path': locals().get('actual_source', source_pbf_path),
            'poly_path': poly_file_path,
            'output_path': output_pbf_path,
            'cpu_core_id': cpu_core_id,
            'job_id': job_id,
            'start_time': start_time,
            'end_time': end_time,
            'duration_seconds': duration_seconds,
            'source_file_size_bytes': os.path.getsize(source_pbf_path) if os.path.exists(source_pbf_path) else 0,
            'output_file_size_bytes': 0,
            'reduction_ratio': 0,
            'processing_rate_mb_per_sec': 0,
            'cache_hit': False,
            'memory_mapped': False,
            'status': 'failed',
            'success': False,
            'error': str(e),
            'osmium_params': extract_params
        }
        
        # Release read access on error
        try:
            if 'best_source' in locals():
                cache_coordinator.concurrent_access.release_read_access(locals()['best_source'], job_id)
        except:
            pass
        
        print(f"[Core {cpu_core_id}] Error during extraction: {e}")
        return metrics
        
    finally:
        used_cores.discard(cpu_core_id)
        
        # Trigger cleanup if needed
        if cache_coordinator.cleanup_manager.should_cleanup():
            print(f"[Core {cpu_core_id}] Triggering cache cleanup...")
            cleanup_stats = cache_coordinator.cleanup_manager.cleanup_cache()
            print(f"[Core {cpu_core_id}] Cleanup freed {cleanup_stats['space_freed_gb']:.2f}GB")
