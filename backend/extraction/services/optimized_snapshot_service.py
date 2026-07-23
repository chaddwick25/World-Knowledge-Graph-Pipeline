"""
Optimized Snapshot Service

Generates filtered daily snapshots from monthly extracts using single-pass tag filtering.
Eliminates redundant time-filtering since monthly extracts are already time-bounded.

Key optimizations:
- Single osmium tags-filter command (no time-filter needed)
- Parallel processing with cores 8-27
- Direct asset generation from filtered snapshots

Author: EDA Vector Search Toolkit
"""
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from api.models import TemporalSnapshot, AssetBundle, PbfFile
from extraction.services.osmium_facade import OsmiumFacade
from extraction.services.asset_generation_service import AssetGenerationService
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json

logger = logging.getLogger(__name__)


class OptimizedSnapshotService:
    """
    Service for generating filtered daily snapshots from monthly extracts.
    
    Workflow:
    1. Take monthly extract as source (already time-bounded to month-end)
    2. Filter by target tags (osmium tags-filter) -> 95%+ size reduction
    3. Create TemporalSnapshot record
    4. Generate AssetBundle for analysis
    
    Key difference from extraction service:
    - Extraction: time-filter only (temporal slicing, no tag filtering)
    - Analysis: tags-filter only (tag filtering, no time slicing needed)
    """
    
    def __init__(self):
        self.osmium_facade = OsmiumFacade()
        self.asset_service = AssetGenerationService()
        self.osmium_executable = getattr(settings, 'OSMIUM_EXECUTABLE', settings.OSMIUM_BINARY_PATH)
        self.cpu_cores = '8-27'  # Use cores 8-27 for parallel processing
        # Use configured temp directory under OSM_WIKIDATA_EXTRACTIONS_DIR
        self.temp_dir = Path(
            getattr(
                settings,
                'OSM_WIKIDATA_TEMP_DIR',
                Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) / 'temp',
            )
        )
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    def _preload_to_ram(self, pbf_path):
        """
        Pre-load PBF file into RAM using Linux page cache.
        This significantly speeds up subsequent osmium operations.
        
        Uses 'cat' to read the entire file, forcing it into page cache.
        With 107 GB RAM available, we can cache multiple monthly extracts.
        """
        try:
            logger.info(f"Pre-loading {pbf_path} into RAM...")
            result = subprocess.run(
                ['cat', str(pbf_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120
            )
            if result.returncode == 0:
                logger.info(f"Successfully pre-loaded {pbf_path} into page cache")
                return True
            else:
                logger.warning(f"Failed to pre-load {pbf_path}: {result.stderr.decode()}")
                return False
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout pre-loading {pbf_path}")
            return False
        except Exception as e:
            logger.warning(f"Error pre-loading {pbf_path}: {e}")
            return False
    
    def _generate_filter_hash(self, tag_key):
        """Generate MD5 hash from tag key for uniqueness."""
        tag_string = json.dumps(tag_key, sort_keys=True)
        return hashlib.md5(tag_string.encode()).hexdigest()
    
    def generate_optimized_daily_snapshots(self, config):
        """
        Generate filtered daily snapshots from monthly extracts using tag-key filtering.
        
        Args:
            config = {
                'monthly_pbf_ids': [uuid1, uuid2, ...],  # PbfFile IDs
                'tag_keys': ['building', 'amenity', 'highway'],  # OSM tag KEYS only
                'name': 'urban_analysis_2025',  # Optional name for AssetBundles
                'output_directory': 'backend/data/filtered_daily_snapshots/<session_id>',
                'session_id': 'uuid-of-session',
                'region_name': 'canada',
                'generate_assets': True,
                'parallel': True,
                'max_workers': 20,
                'cpu_cores': '8-27'
            }
        
        Returns:
            {
                'success': True,
                'snapshots_processed': 12,
                'asset_bundles_created': 36,  # 12 snapshots × 3 tag keys
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
            tag_keys = config.get('tag_keys', [])
            asset_name = config.get('name', None)  # Optional name for AssetBundles
            parallel = config.get('parallel', True)
            max_workers = config.get('max_workers', 20)
            cpu_cores = config.get('cpu_cores', self.cpu_cores)
            
            # Create output directory structure using session_id
            session_dir = output_dir / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            
            # Create temp directory on NVME for filtered PBF files (will be deleted after processing)
            temp_dir = self.temp_dir / f'session_{session_id}'
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Starting optimized snapshot generation for {monthly_pbf_files.count()} monthly PBF files")
            logger.info(f"Tag keys: {tag_keys}")
            logger.info(f"Asset name: {asset_name}")
            logger.info(f"Session ID: {session_id}")
            logger.info(f"Parallel processing: {parallel} (workers: {max_workers}, cores: {cpu_cores})")
            
            results = []
            total_asset_bundles = 0
            
            if parallel:
                # Parallel processing
                results, total_asset_bundles = self._process_parallel(
                    monthly_pbf_files,
                    temp_dir,
                    session_dir,
                    region_name,
                    tag_keys,
                    asset_name,
                    max_workers,
                    cpu_cores,
                    config.get('generate_assets', True)
                )
            else:
                # Sequential processing
                for monthly_pbf in monthly_pbf_files:
                    result = self._process_single_snapshot(
                        monthly_pbf,
                        temp_dir,
                        session_dir,
                        region_name,
                        tag_keys,
                        asset_name,
                        cpu_cores,
                        config.get('generate_assets', True)
                    )
                    if result:
                        results.append(result)
                        total_asset_bundles += result.get('asset_bundles_created', 0)
            
            # Clean up temp directory on NVME
            try:
                import shutil
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned up temp directory on NVME: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")
            
            return {
                'success': True,
                'snapshots_processed': len(results),
                'asset_bundles_created': total_asset_bundles,
                'snapshots': results
            }
            
        except Exception as e:
            logger.error(f"Error in generate_optimized_daily_snapshots: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def _process_parallel(self, monthly_pbf_files, temp_dir, session_dir, region_name, 
                         tag_keys, asset_name, max_workers, cpu_cores, generate_assets):
        """Process snapshots in parallel using ThreadPoolExecutor."""
        results = []
        total_asset_bundles = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            
            for monthly_pbf in monthly_pbf_files:
                future = executor.submit(
                    self._process_single_snapshot,
                    monthly_pbf,
                    temp_dir,
                    session_dir,
                    region_name,
                    tag_keys,
                    asset_name,
                    cpu_cores,
                    generate_assets
                )
                futures[future] = monthly_pbf
            
            for future in as_completed(futures):
                monthly_pbf = futures[future]
                
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        total_asset_bundles += result.get('asset_bundles_created', 0)
                except Exception as e:
                    logger.error(f"Error processing {monthly_pbf.id}: {e}", exc_info=True)
        
        return results, total_asset_bundles
    
    def _process_single_snapshot(self, monthly_pbf, temp_dir, session_dir, region_name,
                                 tag_keys, asset_name, cpu_cores, generate_assets):
        """
        Process a single monthly PBF to create filtered snapshots natively from Parquet files.
        """
        try:
            # Ensure timestamp is timezone-aware
            snapshot_timestamp = monthly_pbf.max_timestamp
            if timezone.is_naive(snapshot_timestamp):
                snapshot_timestamp = timezone.make_aware(snapshot_timestamp)
            
            snapshot_region_name = region_name
            
            # Extract date components
            source_path = Path(monthly_pbf.path)
            stem = source_path.stem.replace('.osm', '')
            parts = stem.split('_')
            
            year, month = None, None
            for i, part in enumerate(parts):
                if part.isdigit() and len(part) == 4:
                    year = part
                    if i + 1 < len(parts) and parts[i + 1].isdigit() and len(parts[i + 1]) == 2:
                        month = parts[i + 1]
                        break
            
            if not year or not month:
                logger.error(f"Could not extract year/month from {monthly_pbf.path}")
                return None
            
            from datetime import datetime, timedelta
            year_int, month_int = int(year), int(month)
            if month_int == 12:
                next_month, next_year = 1, year_int + 1
            else:
                next_month, next_year = month_int + 1, year_int
            last_day = (datetime(next_year, next_month, 1) - timedelta(days=1)).day
            
            # Subdirectories for assets
            region_dir = session_dir / snapshot_region_name / year
            assets_base_dir = region_dir / f"{snapshot_region_name}_{year}_{month}_{last_day:02d}_assets"
            assets_base_dir.mkdir(parents=True, exist_ok=True)
            
            # Locate base Parquet bundle
            base_bundle = AssetBundle.objects.filter(source_monthly_extract=monthly_pbf, tag_key__isnull=True).first()
            if not base_bundle:
                logger.info("Base Parquet bundle not found! Generating it first before filtering...")
                from extraction.services.asset_extractor import extract_assets_from_pbf
                base_dir = assets_base_dir / 'BASE'
                base_dir.mkdir(parents=True, exist_ok=True)
                stats = extract_assets_from_pbf(str(source_path), base_dir)
                
                # Register base snapshot and bundle
                base_snap, _ = TemporalSnapshot.objects.get_or_create(
                    region=snapshot_region_name,
                    timestamp=snapshot_timestamp,
                    snapshot_interval='MONTHLY',
                    filter_hash=None,
                    defaults={'pbf_file': monthly_pbf}
                )
                
                # Fetch total size
                total_size = sum(f.stat().st_size for f in base_dir.glob('*.parquet'))
                
                base_bundle = AssetBundle.objects.create(
                    source_monthly_extract=monthly_pbf,
                    temporal_snapshot=base_snap,
                    bundle_path=str(base_dir),
                    node_count=stats.get('nodes', 0),
                    way_count=stats.get('ways', 0),
                    relation_count=stats.get('relations', 0),
                    edge_count=stats.get('edges', 0),
                    tag_count=stats.get('tags', 0),
                    total_size_bytes=total_size
                )
            
            asset_bundles_created = 0
            asset_bundle_ids = []
            
            for tag_key in tag_keys:
                logger.info(f"Filtering natively across Parquet for tag key '{tag_key}'")
                tag_key_dir = assets_base_dir / tag_key
                tag_key_dir.mkdir(parents=True, exist_ok=True)
                
                # Filter natively using Pandas (bypassing Osmium)
                stats = self._filter_parquet_by_tag_key(base_bundle.bundle_path, tag_key, tag_key_dir)
                
                if not stats:
                    logger.warning(f"No assets found or failed to filter for '{tag_key}'")
                    continue
                
                from django.db import connection
                connection.close()
                
                filter_hash = self._generate_filter_hash(tag_key)
                daily_snapshot, _ = TemporalSnapshot.objects.get_or_create(
                    region=snapshot_region_name,
                    timestamp=snapshot_timestamp,
                    snapshot_interval='MONTHLY',
                    filter_hash=filter_hash,
                    defaults={
                        'pbf_file': monthly_pbf,
                        'filter_config': {'tag_key': tag_key}
                    }
                )
                
                if generate_assets:
                    total_size = sum(f.stat().st_size for f in tag_key_dir.glob('*.parquet'))
                    asset_bundle, _ = AssetBundle.objects.update_or_create(
                        temporal_snapshot=daily_snapshot,
                        tag_key=tag_key,
                        defaults={
                            'bundle_path': str(tag_key_dir),
                            'source_monthly_extract': monthly_pbf,
                            'name': asset_name,
                            'node_count': stats.get('nodes', 0),
                            'way_count': stats.get('ways', 0),
                            'edge_count': stats.get('edges', 0),
                            'tag_count': stats.get('tags', 0),
                            'total_size_bytes': total_size
                        }
                    )
                    asset_bundles_created += 1
                    asset_bundle_ids.append(str(asset_bundle.id))
                    logger.info(f"Created AssetBundle for tag key '{tag_key}': {asset_bundle.id}")
            
            return {
                'monthly_pbf_id': str(monthly_pbf.id),
                'timestamp': snapshot_timestamp.isoformat(),
                'asset_bundles_created': asset_bundles_created,
                'asset_bundle_ids': asset_bundle_ids,
                'assets_directory': str(assets_base_dir)
            }
            
        except Exception as e:
            logger.error(f"Error processing monthly PBF natively: {e}", exc_info=True)
            return None
    
    def _filter_parquet_by_tag_key(self, base_bundle_path, tag_key, output_dir):
        """
        Filters Parquet datasets natively by a tag_key using Pandas.
        """
        import pandas as pd
        base_dir = Path(base_bundle_path)
        
        try:
            # 1. Filter Tags
            tags_df = pd.read_parquet(base_dir / 'tags.parquet')
            filtered_tags = tags_df[tags_df['key'] == tag_key]
            
            if filtered_tags.empty:
                return None
                
            filtered_tags.to_parquet(output_dir / 'tags.parquet', compression='snappy')
            valid_osm_ids = set(filtered_tags['osm_id'])
            
            # 2. Filter Edges
            edges_df = pd.read_parquet(base_dir / 'edges.parquet')
            if 'way_id' in edges_df.columns:
                filtered_edges = edges_df[edges_df['way_id'].isin(valid_osm_ids)]
                num_ways = filtered_edges['way_id'].nunique()
            else:
                filtered_edges = edges_df.head(0)
                num_ways = 0
                
            filtered_edges.to_parquet(output_dir / 'edges.parquet', compression='snappy')
            
            # 3. Filter Nodes
            way_node_ids = set(filtered_edges['osm_id_a']).union(set(filtered_edges['osm_id_b']))
            all_needed_node_ids = valid_osm_ids.union(way_node_ids)
            
            nodes_df = pd.read_parquet(base_dir / 'nodes.parquet')
            filtered_nodes = nodes_df[nodes_df['osm_id'].isin(all_needed_node_ids)]
            filtered_nodes.to_parquet(output_dir / 'nodes.parquet', compression='snappy')
            
            return {
                'nodes': len(filtered_nodes),
                'ways': num_ways,
                'edges': len(filtered_edges),
                'tags': len(filtered_tags),
                'relations': 0
            }
            
        except Exception as e:
            logger.error(f"Error executing Pandas Parquet filtering: {e}", exc_info=True)
            return None
