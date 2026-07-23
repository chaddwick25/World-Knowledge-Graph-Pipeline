from typing import List, Dict, Any
from datetime import datetime
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from .base_ckan_service import BaseCkanService
from ..models import CkanDataset, QualitySnapshot


class CkanQaService(BaseCkanService):
    """Service for fetching and syncing quality scores from CKAN"""
    
    QA_DATASET_ID = "catalogue-quality-scores"
    
    def fetch_qa_scores(self, target_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch quality scores from catalogue-quality-scores dataset
        
        Args:
            target_ids: List of dataset IDs to filter for
            
        Returns:
            List of quality score records
        """
        self.logger.info(f"Fetching QA scores for {len(target_ids)} datasets")
        
        # Fetch the QA dataset metadata
        qa_pkg = self._make_request('package_show', {'id': self.QA_DATASET_ID})
        if not qa_pkg or 'resources' not in qa_pkg:
            self.logger.error("Failed to fetch QA dataset or no resources found")
            return []
        
        # Find JSON resource
        records = []
        for resource in qa_pkg.get('resources', []):
            if resource.get('format', '').lower() != 'json':
                continue
            
            # Download and parse JSON resource
            qa_data = self._download_json_resource(resource.get('url'))
            if qa_data:
                records = self._parse_qa_json(qa_data, target_ids)
                break  # Only process first JSON resource
        
        self.logger.info(f"Found {len(records)} QA score records")
        return records
    
    def _download_json_resource(self, url: str) -> List[Dict[str, Any]]:
        """Download and parse JSON resource file"""
        if not url:
            return []
        
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            response = urllib.request.urlopen(
                req,
                context=self._get_ssl_context(),
                timeout=self.timeout
            )
            import json
            return json.loads(response.read().decode())
        except Exception as e:
            self.logger.error(f"Failed to download JSON resource: {e}")
            return []
    
    def _parse_qa_json(self, qa_data: List[Dict[str, Any]], target_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Parse QA JSON data and filter to target datasets
        
        Args:
            qa_data: Raw QA data from JSON file
            target_ids: Dataset IDs to filter for
            
        Returns:
            List of parsed QA records
        """
        records = []
        for entry in qa_data:
            package_id = entry.get('package')
            if package_id not in target_ids:
                continue
            
            records.append({
                'ckan_id': package_id,
                'score': float(entry.get('score', 0)) * 100,  # Convert to percentage
                'grade': entry.get('grade', 'Unknown'),
                'freshness': float(entry.get('freshness', 0)),
                'metadata': float(entry.get('metadata', 0)),
                'usability': float(entry.get('usability', 0)),
                'completeness': float(entry.get('completeness', 0)),
                'accessibility': float(entry.get('accessibility', 0)),
                'qa_recorded_at': entry.get('recorded_at'),
            })
        
        return records
    
    def create_snapshots(self, qa_records: List[Dict[str, Any]]) -> List[QualitySnapshot]:
        """
        Create QualitySnapshot records from QA data
        
        Args:
            qa_records: List of parsed QA records
            
        Returns:
            List of created QualitySnapshot instances
        """
        snapshots = []
        
        for record in qa_records:
            ckan_id = record.get('ckan_id')
            if not ckan_id:
                continue
            
            try:
                # QA data uses dataset 'name' (slug), not UUID 'ckan_id'
                dataset = CkanDataset.objects.get(name=ckan_id)
            except CkanDataset.DoesNotExist:
                self.logger.warning(f"Dataset {ckan_id} not found, skipping QA snapshot")
                continue
            
            # Parse timestamp
            qa_recorded_at = self._parse_timestamp(record.get('qa_recorded_at'))
            
            snapshot = QualitySnapshot.objects.create(
                dataset=dataset,
                quality_score_pct=record.get('score'),
                grade=record.get('grade', 'Unknown'),
                freshness=record.get('freshness'),
                metadata_score=record.get('metadata'),
                usability=record.get('usability'),
                completeness=record.get('completeness'),
                accessibility=record.get('accessibility'),
                qa_recorded_at=qa_recorded_at,
            )
            snapshots.append(snapshot)
            self.logger.info(f"Created QA snapshot for {dataset.title}: {snapshot.grade}")
        
        return snapshots
    
    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse timestamp string to datetime object"""
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
