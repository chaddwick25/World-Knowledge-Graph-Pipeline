#!/usr/bin/env python3
"""
PBF Cache Manager

Comprehensive caching system for large PBF files with memory mapping,
concurrent access, and intelligent cleanup strategies.
"""

import os
import json
import time
import mmap
import threading
import psutil
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
from django.utils import timezone
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PBFCacheManager:
    """
    Main cache manager for PBF files with memory mapping and LRU tracking.
    """
    
    def __init__(self, cache_dir: str, max_ram_cache_gb: int = 96, max_disk_cache_gb: int = 600):
        self.cache_dir = Path(cache_dir)
        self.max_ram_cache = max_ram_cache_gb * 1024**3
        self.max_disk_cache = max_disk_cache_gb * 1024**3
        
        # Cache storage directories
        self.hot_cache_dir = self.cache_dir / "hot_cache"
        self.warm_cache_dir = self.cache_dir / "warm_cache"
        self.metadata_dir = self.cache_dir / "metadata"
        
        # Create directories
        for dir_path in [self.hot_cache_dir, self.warm_cache_dir, self.metadata_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)
        
        # Memory management
        self.memory_maps: Dict[str, mmap.mmap] = {}
        self.file_handles: Dict[str, object] = {}
        self.access_times: Dict[str, float] = {}
        self.access_counts: Dict[str, int] = defaultdict(int)
        self.cache_sizes: Dict[str, int] = {}
        
        # Thread safety
        self.cache_lock = threading.RLock()
        self.file_locks: Dict[str, threading.RLock] = {}
        
        # Load existing cache metadata
        self.load_cache_metadata()
        
        logger.info(f"PBF Cache Manager initialized:")
        logger.info(f"  Cache directory: {self.cache_dir}")
        logger.info(f"  Max RAM cache: {max_ram_cache_gb}GB")
        logger.info(f"  Max disk cache: {max_disk_cache_gb}GB")
    
    def load_cache_metadata(self):
        """Load cache metadata from disk."""
        metadata_file = self.metadata_dir / "cache_manifest.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)
                    self.access_times = metadata.get('access_times', {})
                    self.access_counts = defaultdict(int, metadata.get('access_counts', {}))
                    self.cache_sizes = metadata.get('cache_sizes', {})
                logger.info(f"Loaded cache metadata: {len(self.access_times)} entries")
            except Exception as e:
                logger.warning(f"Failed to load cache metadata: {e}")
    
    def save_cache_metadata(self):
        """Save cache metadata to disk."""
        metadata_file = self.metadata_dir / "cache_manifest.json"
        try:
            metadata = {
                'access_times': self.access_times,
                'access_counts': dict(self.access_counts),
                'cache_sizes': self.cache_sizes,
                'last_updated': timezone.now().isoformat()
            }
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save cache metadata: {e}")
    
    def get_cache_key(self, pbf_path: str, job_id: str = None) -> str:
        """Generate cache key for PBF file."""
        pbf_name = Path(pbf_path).stem
        if job_id:
            return f"{pbf_name}_{job_id}"
        return pbf_name
    
    def get_cached_pbf_path(self, cache_key: str, cache_tier: str = "warm") -> Path:
        """Get path for cached PBF file."""
        if cache_tier == "hot":
            return self.hot_cache_dir / f"{cache_key}.pbf"
        else:
            return self.warm_cache_dir / f"{cache_key}.pbf"
    
    def is_cached(self, pbf_path: str, job_id: str = None) -> Tuple[bool, Optional[Path]]:
        """Check if PBF file is cached."""
        cache_key = self.get_cache_key(pbf_path, job_id)
        
        # Check hot cache first
        hot_path = self.get_cached_pbf_path(cache_key, "hot")
        if hot_path.exists():
            return True, hot_path
        
        # Check warm cache
        warm_path = self.get_cached_pbf_path(cache_key, "warm")
        if warm_path.exists():
            return True, warm_path
        
        return False, None
    
    def cache_pbf_file(self, source_path: str, cache_key: str, cache_tier: str = "warm") -> Path:
        """Cache a PBF file to specified tier."""
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Source PBF not found: {source_path}")
        
        cached_path = self.get_cached_pbf_path(cache_key, cache_tier)
        
        # Copy file to cache if not already cached
        if not cached_path.exists():
            logger.info(f"Caching {source_path.name} to {cache_tier} cache...")
            shutil.copy2(source_path, cached_path)
            
            # Update metadata
            with self.cache_lock:
                self.cache_sizes[cache_key] = cached_path.stat().st_size
                self.access_times[cache_key] = time.time()
                self.access_counts[cache_key] = 1
        
        return cached_path
    
    def get_memory_mapped_pbf(self, pbf_path: str, job_id: str = None) -> Optional[mmap.mmap]:
        """Get memory-mapped PBF file for shared access."""
        cache_key = self.get_cache_key(pbf_path, job_id)
        
        with self.cache_lock:
            # Check if already memory-mapped
            if cache_key in self.memory_maps:
                self.access_times[cache_key] = time.time()
                self.access_counts[cache_key] += 1
                return self.memory_maps[cache_key]
            
            # Check available RAM
            available_ram = psutil.virtual_memory().available
            file_size = os.path.getsize(pbf_path)
            
            if file_size > available_ram * 0.8:  # Don't use more than 80% of available RAM
                logger.warning(f"File too large for memory mapping: {file_size / 1024**3:.2f}GB")
                return None
            
            # Create memory map
            try:
                file_handle = open(pbf_path, 'rb')
                memory_map = mmap.mmap(file_handle.fileno(), 0, access=mmap.ACCESS_READ)
                
                self.file_handles[cache_key] = file_handle
                self.memory_maps[cache_key] = memory_map
                self.access_times[cache_key] = time.time()
                self.access_counts[cache_key] += 1
                
                logger.info(f"Memory-mapped {Path(pbf_path).name}: {file_size / 1024**3:.2f}GB")
                return memory_map
                
            except Exception as e:
                logger.error(f"Failed to memory-map {pbf_path}: {e}")
                return None
    
    def get_best_cached_source(self, target_poly: str, original_source: str) -> str:
        """Find the best cached source for a polygon extraction."""
        poly_name = Path(target_poly).stem
        
        # Check for region-specific cached extracts
        potential_sources = [
            f"temporal_snapshot_{poly_name}",
            f"continent_{poly_name}",
            f"country_{poly_name}",
            original_source
        ]
        
        for source_key in potential_sources:
            is_cached, cached_path = self.is_cached(source_key)
            if is_cached:
                logger.info(f"Using cached source for {poly_name}: {cached_path.name}")
                return str(cached_path)
        
        return original_source
    
    def promote_to_hot_cache(self, cache_key: str) -> bool:
        """Promote frequently accessed file to hot cache."""
        warm_path = self.get_cached_pbf_path(cache_key, "warm")
        hot_path = self.get_cached_pbf_path(cache_key, "hot")
        
        if warm_path.exists() and not hot_path.exists():
            try:
                # Check hot cache space
                hot_cache_usage = sum(f.stat().st_size for f in self.hot_cache_dir.glob("*.pbf"))
                file_size = warm_path.stat().st_size
                
                if hot_cache_usage + file_size < self.max_ram_cache:
                    shutil.move(warm_path, hot_path)
                    logger.info(f"Promoted {cache_key} to hot cache")
                    return True
                else:
                    logger.warning(f"Hot cache full, cannot promote {cache_key}")
            except Exception as e:
                logger.error(f"Failed to promote {cache_key}: {e}")
        
        return False
    
    def cleanup_memory_maps(self):
        """Clean up unused memory maps."""
        with self.cache_lock:
            current_time = time.time()
            to_remove = []
            
            for cache_key, last_access in self.access_times.items():
                # Remove memory maps not accessed in last hour
                if current_time - last_access > 3600 and cache_key in self.memory_maps:
                    to_remove.append(cache_key)
            
            for cache_key in to_remove:
                try:
                    if cache_key in self.memory_maps:
                        self.memory_maps[cache_key].close()
                        del self.memory_maps[cache_key]
                    
                    if cache_key in self.file_handles:
                        self.file_handles[cache_key].close()
                        del self.file_handles[cache_key]
                    
                    logger.info(f"Cleaned up memory map: {cache_key}")
                except Exception as e:
                    logger.error(f"Error cleaning up {cache_key}: {e}")
    
    def get_cache_stats(self) -> Dict:
        """Get comprehensive cache statistics."""
        hot_cache_size = sum(f.stat().st_size for f in self.hot_cache_dir.glob("*.pbf"))
        warm_cache_size = sum(f.stat().st_size for f in self.warm_cache_dir.glob("*.pbf"))
        memory_mapped_size = sum(len(mm) for mm in self.memory_maps.values())
        
        return {
            'hot_cache_size_gb': hot_cache_size / 1024**3,
            'warm_cache_size_gb': warm_cache_size / 1024**3,
            'memory_mapped_size_gb': memory_mapped_size / 1024**3,
            'hot_cache_files': len(list(self.hot_cache_dir.glob("*.pbf"))),
            'warm_cache_files': len(list(self.warm_cache_dir.glob("*.pbf"))),
            'memory_mapped_files': len(self.memory_maps),
            'total_access_count': sum(self.access_counts.values()),
            'cache_hit_rate': self.calculate_hit_rate()
        }
    
    def calculate_hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total_accesses = sum(self.access_counts.values())
        if total_accesses == 0:
            return 0.0
        
        cache_hits = sum(count for key, count in self.access_counts.items() 
                        if self.is_cached(key)[0])
        return cache_hits / total_accesses
    
    def __del__(self):
        """Cleanup on destruction."""
        try:
            self.save_cache_metadata()
            self.cleanup_memory_maps()
        except Exception as e:
            logger.debug(f"PbfCacheManager cleanup failed: {e}")


class ConcurrentPBFAccess:
    """
    Manages concurrent access to PBF files with reader-writer locks.
    """
    
    def __init__(self, cache_manager: PBFCacheManager):
        self.cache_manager = cache_manager
        self.file_locks: Dict[str, threading.RLock] = {}
        self.reader_counts: Dict[str, int] = defaultdict(int)
        self.active_jobs: Dict[str, Set[str]] = defaultdict(set)
        self.lock = threading.RLock()
    
    def acquire_read_access(self, pbf_path: str, job_id: str) -> Tuple[str, Optional[mmap.mmap]]:
        """Acquire read access to PBF file."""
        cache_key = self.cache_manager.get_cache_key(pbf_path, job_id)
        
        with self.lock:
            # Get or create file lock
            if cache_key not in self.file_locks:
                self.file_locks[cache_key] = threading.RLock()
            
            # Track active job
            self.active_jobs[cache_key].add(job_id)
            self.reader_counts[cache_key] += 1
        
        # Try to get cached version first
        is_cached, cached_path = self.cache_manager.is_cached(pbf_path, job_id)
        if is_cached:
            actual_path = str(cached_path)
        else:
            actual_path = pbf_path
        
        # Get memory-mapped access if possible
        memory_map = self.cache_manager.get_memory_mapped_pbf(actual_path, job_id)
        
        logger.info(f"Job {job_id} acquired read access to {Path(actual_path).name}")
        return actual_path, memory_map
    
    def release_read_access(self, pbf_path: str, job_id: str):
        """Release read access to PBF file."""
        cache_key = self.cache_manager.get_cache_key(pbf_path, job_id)
        
        with self.lock:
            if cache_key in self.active_jobs:
                self.active_jobs[cache_key].discard(job_id)
                self.reader_counts[cache_key] = max(0, self.reader_counts[cache_key] - 1)
        
        logger.info(f"Job {job_id} released read access to {Path(pbf_path).name}")
    
    def get_active_jobs(self, pbf_path: str) -> Set[str]:
        """Get list of jobs currently accessing a PBF file."""
        cache_key = self.cache_manager.get_cache_key(pbf_path)
        return self.active_jobs.get(cache_key, set()).copy()


class CacheCleanupManager:
    """
    Intelligent cache cleanup with LRU and size-based strategies.
    """
    
    def __init__(self, cache_manager: PBFCacheManager):
        self.cache_manager = cache_manager
        self.cleanup_threshold = 0.8  # Start cleanup at 80% capacity
        self.target_usage = 0.6       # Clean down to 60% capacity
    
    def should_cleanup(self) -> bool:
        """Check if cleanup is needed."""
        stats = self.cache_manager.get_cache_stats()
        total_cache_gb = stats['hot_cache_size_gb'] + stats['warm_cache_size_gb']
        max_cache_gb = self.cache_manager.max_disk_cache / 1024**3
        
        return total_cache_gb > (max_cache_gb * self.cleanup_threshold)
    
    def cleanup_cache(self) -> Dict:
        """Perform intelligent cache cleanup."""
        logger.info("Starting cache cleanup...")
        
        cleanup_stats = {
            'files_removed': 0,
            'space_freed_gb': 0,
            'cleanup_duration_seconds': 0
        }
        
        start_time = time.time()
        current_time = time.time()
        
        # Get all cached files with metadata
        cached_files = []
        
        for cache_dir in [self.cache_manager.hot_cache_dir, self.cache_manager.warm_cache_dir]:
            for pbf_file in cache_dir.glob("*.pbf"):
                cache_key = pbf_file.stem
                last_access = self.cache_manager.access_times.get(cache_key, 0)
                access_count = self.cache_manager.access_counts.get(cache_key, 0)
                file_size = pbf_file.stat().st_size
                
                cached_files.append({
                    'path': pbf_file,
                    'cache_key': cache_key,
                    'last_access': last_access,
                    'access_count': access_count,
                    'file_size': file_size,
                    'age_hours': (current_time - last_access) / 3600,
                    'is_hot_cache': cache_dir == self.cache_manager.hot_cache_dir
                })
        
        # Sort by cleanup priority (LRU + access frequency + size)
        def cleanup_priority(file_info):
            age_score = file_info['age_hours']
            frequency_score = 1.0 / max(1, file_info['access_count'])
            size_score = file_info['file_size'] / 1024**3  # GB
            hot_penalty = -10 if file_info['is_hot_cache'] else 0  # Keep hot cache longer
            
            return age_score + frequency_score + size_score + hot_penalty
        
        cached_files.sort(key=cleanup_priority, reverse=True)
        
        # Remove files until we reach target usage
        target_size = self.cache_manager.max_disk_cache * self.target_usage
        current_size = sum(f['file_size'] for f in cached_files)
        
        for file_info in cached_files:
            if current_size <= target_size:
                break
            
            try:
                # Don't remove files accessed in last hour
                if file_info['age_hours'] < 1:
                    continue
                
                file_path = file_info['path']
                file_size = file_info['file_size']
                
                # Remove file
                file_path.unlink()
                
                # Update tracking
                current_size -= file_size
                cleanup_stats['files_removed'] += 1
                cleanup_stats['space_freed_gb'] += file_size / 1024**3
                
                # Clean up metadata
                cache_key = file_info['cache_key']
                self.cache_manager.access_times.pop(cache_key, None)
                self.cache_manager.access_counts.pop(cache_key, None)
                self.cache_manager.cache_sizes.pop(cache_key, None)
                
                logger.info(f"Removed cached file: {file_path.name}")
                
            except Exception as e:
                logger.error(f"Error removing {file_info['path']}: {e}")
        
        cleanup_stats['cleanup_duration_seconds'] = time.time() - start_time
        
        # Clean up memory maps
        self.cache_manager.cleanup_memory_maps()
        
        # Save updated metadata
        self.cache_manager.save_cache_metadata()
        
        logger.info(f"Cache cleanup completed: {cleanup_stats}")
        return cleanup_stats


class MultiJobCacheCoordinator:
    """
    Coordinates cache access across multiple concurrent jobs.
    """
    
    def __init__(self, cache_manager: PBFCacheManager):
        self.cache_manager = cache_manager
        self.concurrent_access = ConcurrentPBFAccess(cache_manager)
        self.cleanup_manager = CacheCleanupManager(cache_manager)
        self.job_registry: Dict[str, Dict] = {}
    
    def register_job(self, job_id: str, job_config: Dict):
        """Register a processing job."""
        self.job_registry[job_id] = {
            'config': job_config,
            'start_time': time.time(),
            'status': 'registered',
            'accessed_files': set()
        }
        logger.info(f"Registered job: {job_id}")
    
    def coordinate_pbf_access(self, jobs: List[Dict]) -> Dict[str, str]:
        """Coordinate PBF access across multiple jobs."""
        # Group jobs by source PBF
        pbf_groups = defaultdict(list)
        for job in jobs:
            pbf_path = job['source_pbf']
            pbf_groups[pbf_path].append(job)
        
        access_plan = {}
        
        for pbf_path, job_group in pbf_groups.items():
            if len(job_group) > 1:
                # Multiple jobs - set up shared caching
                logger.info(f"Setting up shared cache for {len(job_group)} jobs on {Path(pbf_path).name}")
                
                # Pre-cache the file
                cache_key = self.cache_manager.get_cache_key(pbf_path, "shared")
                cached_path = self.cache_manager.cache_pbf_file(pbf_path, cache_key, "hot")
                
                # Memory map for shared access
                self.cache_manager.get_memory_mapped_pbf(str(cached_path), "shared")
                
                for job in job_group:
                    access_plan[job['job_id']] = str(cached_path)
            else:
                # Single job - direct access
                job = job_group[0]
                access_plan[job['job_id']] = pbf_path
        
        return access_plan
    
    def get_system_stats(self) -> Dict:
        """Get comprehensive system and cache statistics."""
        cache_stats = self.cache_manager.get_cache_stats()
        
        system_stats = {
            'memory_usage_gb': psutil.virtual_memory().used / 1024**3,
            'memory_available_gb': psutil.virtual_memory().available / 1024**3,
            'disk_usage_gb': shutil.disk_usage(self.cache_manager.cache_dir)[1] / 1024**3,
            'active_jobs': len(self.job_registry),
            'cpu_percent': psutil.cpu_percent(),
        }
        
        return {**cache_stats, **system_stats}
