import logging
from pathlib import Path
from datetime import datetime
from django.conf import settings
from api.models import PbfFile
from extraction.services.asset_extractor import extract_assets_from_pbf

logger = logging.getLogger(__name__)

class AssetGenerationService:
    def __init__(self, pbf_file_id: str = None):
        if pbf_file_id:
            try:
                self.pbf_file = PbfFile.objects.get(id=pbf_file_id)
            except PbfFile.DoesNotExist:
                raise ValueError(f"PBF file with ID {pbf_file_id} not found.")
        else:
            self.pbf_file = None
        
        self.base_asset_dir = getattr(
            settings,
            'ASSET_BUNDLE_DIR',
            str(Path(settings.BASE_DATA_DIR) / 'assets'),
        )

    def _create_asset_directory(self) -> Path:
        """Creates a unique, versioned directory for the asset bundle."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.pbf_file:
            stem = Path(self.pbf_file.path).stem
        else:
            stem = "asset_bundle"
        dir_name = f"{stem}_{timestamp}"
        asset_path = Path(self.base_asset_dir) / dir_name
        asset_path.mkdir(parents=True, exist_ok=True)
        return asset_path

    def generate_assets(self):
        """Orchestrates the end-to-end asset generation workflow."""
        if not self.pbf_file:
            raise ValueError("PBF file is required for generate_assets")

        asset_path = self._create_asset_directory()

        asset_bundle = ProcessedAssetBundle.objects.create(
            source_pbf=self.pbf_file,
            status=ProcessedAssetBundle.Status.PROCESSING,
            asset_directory=str(asset_path.relative_to(self.base_asset_dir)),
            generation_config={'extractor': 'asset_extractor.py', 'method': 'pyosmium'}
        )

        try:
            logger.info(f"Starting asset extraction from PBF: {self.pbf_file.path}")
            
            # Use the Python-based osmium extractor
            stats = extract_assets_from_pbf(
                pbf_path=str(self.pbf_file.path),
                output_dir=asset_path
            )
            
            # Update the generation config with extraction statistics
            asset_bundle.generation_config['statistics'] = stats
            asset_bundle.status = ProcessedAssetBundle.Status.COMPLETED
            asset_bundle.save()
            
            logger.info(f"Successfully generated assets in: {asset_path}")
            logger.info(f"Extraction statistics: {stats}")

        except Exception as e:
            asset_bundle.status = ProcessedAssetBundle.Status.FAILED
            asset_bundle.save()
            error_message = f"Asset generation failed: {str(e)}"
            logger.error(error_message, exc_info=True)
            raise RuntimeError(error_message) from e
        
        return asset_bundle

    def generate_from_snapshot(self, config: dict) -> dict:
        """
        Generate AssetBundle from TemporalSnapshot.
        Creates 1:1 relationship.
        
        Args:
            config: {
                'snapshot_id': UUID,
                'output_directory': str
            }
        
        Returns:
            {
                'success': bool,
                'asset_bundle_id': str,
                'bundle_path': str,
                'node_count': int,
                'way_count': int,
                ...
            }
        """
        from api.models import TemporalSnapshot, AssetBundle
        
        try:
            snapshot_id = config.get('snapshot_id')
            if not snapshot_id:
                return {'success': False, 'error': 'snapshot_id is required'}
                
            snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
            
            # Check if AssetBundle already exists
            regenerate = config.get('regenerate', False)
            if hasattr(snapshot, 'asset_bundle'):
                if not regenerate:
                    return {
                        'success': False,
                        'error': 'AssetBundle already exists for this snapshot'
                    }
                else:
                    # Delete existing AssetBundle to regenerate
                    logger.info(f"Regenerating assets for snapshot {snapshot_id}")
                    snapshot.asset_bundle.delete()
            
            # Generate assets from snapshot's source extract
            extract = snapshot.pbf_file
            
            # Create output directory
            # Structure: data/assets/region/interval/year/region_date_assets
            output_base = config.get('output_directory', 'data/assets')
            output_dir = Path(output_base) / snapshot.region / snapshot.snapshot_interval.lower() / str(snapshot.timestamp.year) / f"{snapshot.region}_{snapshot.timestamp.strftime('%Y_%m_%d')}_assets"
            
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # For DAILY_FILTERED snapshots, use the file directly (already filtered)
            # For other snapshots, filter from source
            import time
            
            if snapshot.snapshot_interval == 'DAILY_FILTERED':
                # Use the already-filtered snapshot file directly
                source_pbf_path = Path(snapshot.file_path)
                
                if not source_pbf_path.exists():
                    return {
                        'success': False,
                        'error': f'Snapshot file not found: {snapshot.file_path}'
                    }
                
                logger.info(f"Extracting assets from filtered snapshot: {source_pbf_path}")
                
                try:
                    start_time = time.time()
                    # Extract assets directly from filtered snapshot
                    stats = extract_assets_from_pbf(
                        pbf_path=str(source_pbf_path),
                        output_dir=output_dir
                    )
                    duration = time.time() - start_time
                except Exception as e:
                    return {
                        'success': False,
                        'error': f'Asset extraction failed: {str(e)}'
                    }
            else:
                # For non-filtered snapshots, use time-filter from source
                from extraction.services.osmium_facade import OsmiumFacade
                osmium_facade = OsmiumFacade()
                
                # Define temp pbf path
                temp_pbf_path = output_dir / f"temp_{snapshot.id}.osm.pbf"
                timestamp_iso = snapshot.timestamp.isoformat()
                
                logger.info(f"Filtering PBF {extract.path} to timestamp {timestamp_iso} for snapshot {snapshot.id}")
                
                # Run time-filter to get data valid at timestamp
                filter_result = osmium_facade.time_filter(str(extract.path), timestamp_iso, str(temp_pbf_path))
                
                if filter_result.get('exit_code') != 0:
                    return {
                        'success': False, 
                        'error': f"Time filter failed: {filter_result.get('error')}"
                    }
                
                try:
                    start_time = time.time()
                    # Extract assets from temp PBF
                    stats = extract_assets_from_pbf(
                        pbf_path=str(temp_pbf_path),
                        output_dir=output_dir
                    )
                    duration = time.time() - start_time
                except Exception as e:
                    return {
                        'success': False,
                        'error': f'Asset extraction failed: {str(e)}'
                    }
                finally:
                    # Clean up temp file
                    if temp_pbf_path.exists():
                        temp_pbf_path.unlink()
            
            try:
                
                # Create AssetBundle record
                asset_bundle = AssetBundle.objects.create(
                    temporal_snapshot=snapshot,
                    bundle_path=str(output_dir),
                    node_count=stats.get('nodes', 0),
                    way_count=stats.get('ways', 0),
                    relation_count=stats.get('relations', 0),
                    edge_count=stats.get('edges', 0),
                    tag_count=stats.get('tags', 0),
                    generation_time_seconds=duration
                )
                
                logger.info(f"Created AssetBundle: {output_dir}")
                logger.info(f"Asset stats: {stats}")
                
                return {
                    'success': True,
                    'asset_bundle_id': str(asset_bundle.id),
                    'bundle_path': str(output_dir),
                    'node_count': asset_bundle.node_count,
                    'way_count': asset_bundle.way_count,
                    'relation_count': asset_bundle.relation_count,
                    'edge_count': asset_bundle.edge_count,
                    'tag_count': asset_bundle.tag_count
                }
                
            finally:
                # Clean up temp file (only exists for non-filtered snapshots)
                if 'temp_pbf_path' in locals() and temp_pbf_path.exists():
                    try:
                        temp_pbf_path.unlink()
                    except OSError as e:
                        logger.warning(f"Failed to delete temp PBF {temp_pbf_path}: {e}")

        except Exception as e:
             logger.error(f"Error generating AssetBundle: {e}", exc_info=True)
             return {'success': False, 'error': str(e)}
    
    def generate_from_pbf(self, config: dict) -> dict:
        """
        Generate AssetBundle directly from a PBF file with tag_key and name support.
        
        Args:
            config: {
                'pbf_path': str,  # Path to PBF file
                'output_directory': str,  # Output directory for assets
                'snapshot_id': str,  # TemporalSnapshot ID
                'tag_key': str,  # OSM tag key (e.g., 'building', 'amenity')
                'name': str,  # Optional name for the AssetBundle
                'source_monthly_extract_id': str  # Source monthly extract PbfFile ID
            }
        
        Returns:
            {
                'success': bool,
                'asset_bundle_id': str,
                'bundle_path': str,
                'node_count': int,
                ...
            }
        """
        from api.models import TemporalSnapshot, AssetBundle, PbfFile
        import time
        
        try:
            pbf_path = config.get('pbf_path')
            output_dir = Path(config.get('output_directory'))
            snapshot_id = config.get('snapshot_id')
            tag_key = config.get('tag_key')
            asset_name = config.get('name')
            source_monthly_extract_id = config.get('source_monthly_extract_id')
            
            if not pbf_path or not output_dir or not snapshot_id:
                return {
                    'success': False,
                    'error': 'pbf_path, output_directory, and snapshot_id are required'
                }
            
            pbf_path = Path(pbf_path)
            if not pbf_path.exists():
                return {
                    'success': False,
                    'error': f'PBF file not found: {pbf_path}'
                }
            
            snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
            
            # Get source monthly extract if provided
            source_monthly_extract = None
            if source_monthly_extract_id:
                try:
                    source_monthly_extract = PbfFile.objects.get(id=source_monthly_extract_id)
                except PbfFile.DoesNotExist:
                    logger.warning(f"Source monthly extract {source_monthly_extract_id} not found")
            
            output_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Extracting assets from {pbf_path} to {output_dir}")
            
            try:
                start_time = time.time()
                # Extract assets from PBF
                stats = extract_assets_from_pbf(
                    pbf_path=str(pbf_path),
                    output_dir=output_dir
                )
                duration = time.time() - start_time
            except Exception as e:
                return {
                    'success': False,
                    'error': f'Asset extraction failed: {str(e)}'
                }
            
            # Calculate total size of asset files
            total_size = 0
            for file_path in output_dir.glob('*.tsv'):
                total_size += file_path.stat().st_size
            
            # Create or update AssetBundle record
            # Use update_or_create to handle cases where AssetBundle already exists
            asset_bundle, created = AssetBundle.objects.update_or_create(
                temporal_snapshot=snapshot,
                tag_key=tag_key,
                defaults={
                    'bundle_path': str(output_dir),
                    'node_count': stats.get('nodes', 0),
                    'way_count': stats.get('ways', 0),
                    'relation_count': stats.get('relations', 0),
                    'edge_count': stats.get('edges', 0),
                    'tag_count': stats.get('tags', 0),
                    'total_size_bytes': total_size,
                    'generation_time_seconds': duration,
                    'source_monthly_extract': source_monthly_extract,
                    'name': asset_name
                }
            )
            
            action = "Created" if created else "Updated"
            logger.info(f"{action} AssetBundle: {asset_bundle.id} (tag_key={tag_key}, name={asset_name})")
            logger.info(f"Asset stats: {stats}")
            
            return {
                'success': True,
                'asset_bundle_id': str(asset_bundle.id),
                'bundle_path': str(output_dir),
                'node_count': asset_bundle.node_count,
                'way_count': asset_bundle.way_count,
                'relation_count': asset_bundle.relation_count,
                'edge_count': asset_bundle.edge_count,
                'tag_count': asset_bundle.tag_count,
                'tag_key': tag_key,
                'name': asset_name
            }
            
        except Exception as e:
            logger.error(f"Error generating AssetBundle from PBF: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

