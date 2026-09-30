import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..adapters.base import BaseSourceAdapter
from ..adapters.ckan_adapter import CkanAdapter
from ..models import (
    DataSource,
    GeoDataset,
    GeoRecord,
    GeoResource,
    IngestionState,
)

# Factory registry: adapter_type → adapter class. Phases 2–5 of the
# GEODATA_APP_REFACTOR_PLAN add wfs/socrata/direct/rest here.
ADAPTER_REGISTRY = {
    'ckan': CkanAdapter,
}


@dataclass
class IngestionResult:
    """Outcome of a ``download_and_ingest`` run."""
    dataset_name: str = ''
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    records_ingested: int = 0
    records_deleted: int = 0
    errors_list: List[str] = field(default_factory=list)


class IngestionService:
    """Orchestrates dataset discovery, download, and ingestion.

    Refactored from the legacy ``toronto_data`` services
    (``CkanMetadataService`` + ``DatasetDownloadService``) with the
    Toronto-specific typed models replaced by the generic
    :class:`GeoRecord` store.
    """

    def __init__(self, source: DataSource):
        self.source = source
        self.adapter = self._get_adapter(source)
        self.logger = logging.getLogger(self.__class__.__name__)
        from django.conf import settings
        cfg = getattr(settings, 'GEODATA_CONFIG', {})
        self.bulk_batch_size = int(cfg.get('bulk_insert_batch_size', 1000))

    @staticmethod
    def _get_adapter(source: DataSource) -> BaseSourceAdapter:
        adapter_cls = ADAPTER_REGISTRY.get(source.adapter_type)
        if adapter_cls is None:
            raise ValueError(
                f"No adapter registered for adapter_type '{source.adapter_type}'"
            )
        return adapter_cls(source)

    # ------------------------------------------------------------------
    # Metadata sync
    # ------------------------------------------------------------------

    def sync_metadata(self, dataset_ids: Optional[List[str]] = None) -> int:
        """Fetch and persist dataset/resource metadata from the source.

        Args:
            dataset_ids: Dataset IDs to sync; defaults to the source's
                ``config.target_datasets``.

        Returns:
            Number of datasets successfully synced.
        """
        if dataset_ids is None:
            dataset_ids = list(self.source.config.get('target_datasets', []))
        if not dataset_ids:
            self.logger.warning(
                f"No target datasets for source '{self.source.name}' "
                f"(pass --datasets or set config.target_datasets)"
            )
            return 0

        synced = 0
        for dataset_id in dataset_ids:
            metadata = self.adapter.fetch_dataset_metadata(dataset_id)
            if not metadata:
                self.logger.error(f"No metadata returned for {dataset_id}")
                continue
            self._upsert_dataset(metadata)
            synced += 1
            if self.adapter.request_delay:
                time.sleep(self.adapter.request_delay)

        self.logger.info(f"Synced metadata for {synced}/{len(dataset_ids)} datasets")
        return synced

    def _upsert_dataset(self, metadata: Dict[str, Any]) -> GeoDataset:
        dataset_id = metadata.get('id') or metadata.get('name')
        if not dataset_id:
            raise ValueError("No id or name in metadata")

        dataset, created = GeoDataset.objects.update_or_create(
            source=self.source,
            source_dataset_id=dataset_id,
            defaults={
                'title': metadata.get('title') or '',
                'name': metadata.get('name') or '',
                'description': metadata.get('notes') or '',
                'publisher': (metadata.get('organization') or {}).get('title') or '',
                'license': metadata.get('license_title') or '',
                'refresh_rate': metadata.get('refresh_rate') or 'Unknown',
                'is_retired': bool(metadata.get('is_retired', False)),
                'metadata': metadata,
            },
        )
        action = "Created" if created else "Updated"
        self.logger.info(f"{action} dataset: {dataset.title}")

        self._upsert_resources(dataset, metadata.get('resources', []))
        return dataset

    def _upsert_resources(self, dataset: GeoDataset, resources: List[Dict[str, Any]]) -> int:
        synced = 0
        for resource_data in resources:
            resource_id = resource_data.get('id')
            if not resource_id:
                continue
            GeoResource.objects.update_or_create(
                source_resource_id=resource_id,
                defaults={
                    'dataset': dataset,
                    'name': resource_data.get('name') or '',
                    'format': (resource_data.get('format') or '').upper(),
                    'url': resource_data.get('url') or '',
                    'size_bytes': resource_data.get('size'),
                    'mimetype': resource_data.get('mimetype') or '',
                    'last_modified': self._parse_timestamp(resource_data.get('last_modified')),
                    'metadata': resource_data,
                },
            )
            synced += 1
        self.logger.info(f"Synced {synced} resources for {dataset.title}")
        return synced

    # ------------------------------------------------------------------
    # Download + ingest
    # ------------------------------------------------------------------

    def download_and_ingest(
        self,
        dataset_name: str,
        *,
        force: bool = False,
        incremental: bool = False,
        truncate: bool = True,
    ) -> IngestionResult:
        """Download a dataset's resources and ingest records into GeoRecord.

        Args:
            dataset_name: GeoDataset ``name`` slug or ``source_dataset_id``.
            force: Force re-download even if the file is up to date.
            incremental: Only ingest records newer than the last ingestion.
            truncate: Delete existing records for each resource before
                importing (default True). Ignored when incremental.
        """
        dataset = (
            GeoDataset.objects.filter(source=self.source, name=dataset_name).first()
            or GeoDataset.objects.filter(source=self.source, source_dataset_id=dataset_name).first()
        )
        if dataset is None:
            raise ValueError(f"Dataset not found: {dataset_name}")

        result = IngestionResult(dataset_name=dataset.name)
        resources = dataset.resources.filter(format__in=['CSV', 'JSON', 'GEOJSON'])
        if not resources.exists():
            self.logger.warning(f"No CSV/JSON resources for dataset {dataset.name}")

        for resource in resources:
            try:
                file_path = self.adapter.download_resource(resource, force=force)
                if file_path is None:
                    result.skipped += 1
                    continue
                result.downloaded += 1
                ingested = self._ingest_resource(
                    resource,
                    file_path,
                    truncate=truncate and not incremental,
                    incremental=incremental,
                )
                result.records_ingested += ingested
            except Exception as e:
                result.errors += 1
                result.errors_list.append(f"{resource.name}: {e}")
                self.logger.exception(f"Error ingesting resource {resource.name}")

        self.logger.info(
            f"Ingest for {dataset.name}: downloaded={result.downloaded}, "
            f"skipped={result.skipped}, errors={result.errors}, "
            f"records={result.records_ingested}"
        )
        return result

    def _ingest_resource(
        self,
        resource: GeoResource,
        file_path: Path,
        truncate: bool = True,
        incremental: bool = False,
    ) -> int:
        date_field = self._date_field_for(resource.dataset)

        ingestion_state = None
        if incremental:
            ingestion_state, _ = IngestionState.objects.get_or_create(resource=resource)
            last = ingestion_state.last_ingested_date or ingestion_state.last_ingested_datetime
            self.logger.info(f"Incremental mode: last ingested = {last or 'Never'}")

        if truncate:
            deleted, _ = GeoRecord.objects.filter(resource=resource).delete()
            self.logger.info(f"Deleted {deleted} existing records for {resource.name}")
        else:
            deleted = 0

        rows = []
        skipped = 0
        latest_date = None
        for record in self.adapter.parse_resource(file_path, date_field=date_field):
            record_date = record.get('ingestion_date')
            if self._should_skip_record(record_date, ingestion_state):
                skipped += 1
                continue
            if record_date and (not latest_date or record_date > latest_date):
                latest_date = record_date

            rows.append(GeoRecord(
                dataset=resource.dataset,
                resource=resource,
                source_id=record.get('source_id', '')[:200],
                attributes=record.get('attributes', {}),
                geom=record.get('geom'),
                ingestion_date=record_date,
            ))
            if len(rows) >= self.bulk_batch_size:
                GeoRecord.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                rows = []

        ingested = len(rows)
        if rows:
            GeoRecord.objects.bulk_create(rows, batch_size=self.bulk_batch_size)

        if skipped > 0:
            self.logger.info(f"Skipped {skipped} records (already ingested)")

        mode = 'incremental' if incremental else 'full'
        self._update_ingestion_state(ingestion_state, latest_date, ingested, mode)
        return ingested

    def _date_field_for(self, dataset: GeoDataset) -> Optional[str]:
        """Per-source date-field mapping, e.g. ``dataset_date_fields``."""
        return self.source.config.get('dataset_date_fields', {}).get(dataset.name)

    # ------------------------------------------------------------------
    # Incremental ingestion state helpers
    # ------------------------------------------------------------------

    def _should_skip_record(
        self,
        record_date: Optional[date],
        ingestion_state: Optional[IngestionState],
    ) -> bool:
        """Skip records on or before the last ingested date."""
        if not ingestion_state or not record_date:
            return False
        last_date = ingestion_state.last_ingested_date
        if not last_date:
            return False
        return record_date <= last_date

    def _update_ingestion_state(
        self,
        ingestion_state: Optional[IngestionState],
        latest_date: Optional[date],
        records_count: int,
        mode: str,
    ):
        if not ingestion_state:
            return
        if latest_date and (
            not ingestion_state.last_ingested_date
            or latest_date > ingestion_state.last_ingested_date
        ):
            ingestion_state.last_ingested_date = latest_date
        ingestion_state.last_ingested_count = records_count
        ingestion_state.total_records_ingested += records_count
        ingestion_state.last_ingestion_mode = mode
        ingestion_state.save()
        self.logger.info(
            f"Updated ingestion state: latest_date={latest_date}, "
            f"count={records_count}, total={ingestion_state.total_records_ingested}"
        )

    # ------------------------------------------------------------------
    # Timestamp helper
    # ------------------------------------------------------------------

    def _parse_timestamp(self, timestamp_str: str) -> Any:
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
