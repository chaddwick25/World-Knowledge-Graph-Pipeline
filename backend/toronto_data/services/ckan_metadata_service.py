from typing import Dict, List, Any
from datetime import datetime
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .base_ckan_service import BaseCkanService
from ..models import CkanDataset, CkanResource


class CkanMetadataService(BaseCkanService):
    """Service for fetching and syncing CKAN dataset metadata"""
    
    TARGET_DATASETS = [
        # Existing datasets
        "traffic-volumes-at-intersections-for-all-modes",
        "ttc-subway-delay-data",
        "cafeto-curb-lane-parklet-cafe-locations",
        
        # Spatial Infrastructure (Graph Foundation)
        "toronto-centreline-tcl",
        "intersection-file-city-of-toronto",
        "cycling-network",
        "neighbourhoods",
        "zoning-by-law",
        "business-improvement-areas",
        
        # Temporal Flows (Time-Series Data)
        "permanent-bicycle-counters",
        "ttc-routes-and-schedules",
        "rain-gauge-locations-and-precipitation",
        "preliminary-zoning-reviews",
        "neighbourhood-profiles",
        
        # Additional Datasets
        "forest-and-land-cover",
        "committee-of-adjustment-applications",
    ]
    
    def fetch_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """
        Fetch metadata for a single dataset using package_show action
        
        Args:
            dataset_id: CKAN package identifier
            
        Returns:
            Package metadata dictionary
        """
        self.logger.info(f"Fetching metadata for dataset: {dataset_id}")
        return self._make_request('package_show', {'id': dataset_id})
    
    def fetch_multiple(self, dataset_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch metadata for multiple datasets
        
        Args:
            dataset_ids: List of CKAN package identifiers
            
        Returns:
            List of package metadata dictionaries
        """
        results = []
        for dataset_id in dataset_ids:
            metadata = self.fetch_metadata(dataset_id)
            if metadata:
                results.append(metadata)
        return results
    
    def sync_to_database(self, metadata: Dict[str, Any]) -> CkanDataset:
        """
        Create or update CkanDataset and CkanResource records from metadata
        
        Args:
            metadata: Package metadata from CKAN API
            
        Returns:
            CkanDataset instance
        """
        if not metadata:
            raise ValueError("Empty metadata provided")
        
        ckan_id = metadata.get('id') or metadata.get('name')
        if not ckan_id:
            raise ValueError("No id or name in metadata")
        
        # Parse timestamps
        metadata_created = self._parse_timestamp(metadata.get('metadata_created'))
        metadata_modified = self._parse_timestamp(metadata.get('metadata_modified'))
        
        # Create or update dataset
        dataset, created = CkanDataset.objects.update_or_create(
            ckan_id=ckan_id,
            defaults={
                'title': metadata.get('title', ''),
                'name': metadata.get('name', ''),
                'notes': metadata.get('notes', ''),
                'refresh_rate': metadata.get('refresh_rate', 'Unknown'),
                'is_retired': bool(metadata.get('is_retired', False)),
                'owner_org': metadata.get('owner_org', ''),
                'metadata_created': metadata_created,
                'metadata_modified': metadata_modified,
            }
        )
        
        action = "Created" if created else "Updated"
        self.logger.info(f"{action} dataset: {dataset.title}")
        
        # Sync resources
        resources = metadata.get('resources', [])
        self._sync_resources(dataset, resources)
        
        return dataset
    
    def _sync_resources(self, dataset: CkanDataset, resources: List[Dict[str, Any]]):
        """
        Sync resource records for a dataset
        
        Args:
            dataset: CkanDataset instance
            resources: List of resource dictionaries from CKAN
        """
        for resource_data in resources:
            resource_id = resource_data.get('id')
            if not resource_id:
                continue
            
            last_modified = self._parse_timestamp(resource_data.get('last_modified'))
            
            CkanResource.objects.update_or_create(
                ckan_resource_id=resource_id,
                defaults={
                    'dataset': dataset,
                    'name': resource_data.get('name', ''),
                    'format': resource_data.get('format', '').upper(),
                    'url': resource_data.get('url', ''),
                    'size': resource_data.get('size'),
                    'mimetype': resource_data.get('mimetype', ''),
                    'last_modified': last_modified,
                }
            )
        
        self.logger.info(f"Synced {len(resources)} resources for {dataset.title}")
    
    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse CKAN timestamp string to datetime object"""
        if not timestamp_str:
            return None
        
        try:
            dt = parse_datetime(timestamp_str)
            if dt and timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except Exception as e:
            self.logger.warning(f"Failed to parse timestamp '{timestamp_str}': {e}")
            return None
