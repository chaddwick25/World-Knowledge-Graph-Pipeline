import logging
from datetime import datetime
from typing import Any, Dict, List

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..adapters.base import BaseSourceAdapter
from ..models import DataQualitySnapshot, DataSource, GeoDataset
from .ingestion_service import IngestionService


class QualityService:
    """Fetches and persists quality scores for datasets.

    Refactored from the legacy ``toronto_data.services.ckan_qa_service``
    (``CkanQaService``) and generalized for all sources.
    """

    def __init__(self, source: DataSource, adapter: BaseSourceAdapter = None):
        self.source = source
        self.adapter = adapter or IngestionService._get_adapter(source)
        self.logger = logging.getLogger(self.__class__.__name__)

    def sync_quality_snapshots(self, dataset_ids: List[str]) -> List[DataQualitySnapshot]:
        """Fetch QA records from the adapter and persist snapshots."""
        records = self.adapter.fetch_quality_records(dataset_ids)
        return self.create_snapshots(records)

    def create_snapshots(self, qa_records: List[Dict[str, Any]]) -> List[DataQualitySnapshot]:
        """Create DataQualitySnapshot rows from parsed QA records.

        Idempotent per ``(dataset, qa_recorded_at)`` — re-syncing the same
        QA payload updates existing snapshots instead of duplicating them.
        """
        snapshots = []
        for record in qa_records:
            dataset_id = record.get('dataset_id')
            if not dataset_id:
                continue

            # QA data references datasets by slug (name); fall back to
            # the source_dataset_id (UUID) lookup.
            dataset = (
                GeoDataset.objects.filter(source=self.source, name=dataset_id).first()
                or GeoDataset.objects.filter(source=self.source, source_dataset_id=dataset_id).first()
            )
            if dataset is None:
                self.logger.warning(f"Dataset {dataset_id} not found, skipping QA snapshot")
                continue

            qa_recorded_at = self._parse_timestamp(record.get('qa_recorded_at'))
            defaults = {
                'quality_score_pct': record.get('score'),
                'grade': record.get('grade') or 'Unknown',
                'freshness': record.get('freshness'),
                'metadata_score': record.get('metadata_score'),
                'usability': record.get('usability'),
                'completeness': record.get('completeness'),
                'accessibility': record.get('accessibility'),
            }
            if qa_recorded_at:
                snapshot, _ = DataQualitySnapshot.objects.update_or_create(
                    dataset=dataset,
                    qa_recorded_at=qa_recorded_at,
                    defaults=defaults,
                )
            else:
                snapshot = DataQualitySnapshot.objects.create(
                    dataset=dataset,
                    qa_recorded_at=None,
                    **defaults,
                )
            snapshots.append(snapshot)
            self.logger.info(f"Created QA snapshot for {dataset.title}: {snapshot.grade}")

        return snapshots

    def _parse_timestamp(self, timestamp_str: str) -> datetime:
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
