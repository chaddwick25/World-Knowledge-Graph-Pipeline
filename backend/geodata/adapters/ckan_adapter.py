import csv
import io
import json
import logging
import ssl
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point
from django.utils import timezone
from django.utils.dateparse import parse_date

from ..models import GeoResource
from .base import BaseSourceAdapter

# Increase CSV field size limit for large geometry fields
csv.field_size_limit(10485760)  # 10MB


class CkanAdapter(BaseSourceAdapter):
    """CKAN API adapter — works with Toronto, Vancouver, Ottawa, Montreal,
    Open Canada.

    Refactored from the legacy ``toronto_data`` services:
    ``BaseCkanService`` + ``CkanMetadataService`` + ``CkanQaService`` +
    ``DatasetDownloadService``. Per-source tuning lives in
    ``DataSource.config`` (e.g. ``target_datasets``, ``qa_dataset_id``,
    ``request_delay_seconds``).
    """

    # Common date column names used for temporal datasets (in priority order).
    # Note: Toronto's TTC datasets use mixed-case 'Date'.
    DATE_FIELD_CANDIDATES = [
        'count_date', 'date', 'Date', 'DATE', 'application_date',
        'measurement_date', 'latest_count_date', 'MEASUREMENT_DATE',
    ]

    def __init__(self, source):
        super().__init__(source)
        cfg = getattr(settings, 'GEODATA_CONFIG', {})
        self.logger = logging.getLogger(self.__class__.__name__)
        self.base_url = source.base_url
        self.api_version = str(self.config.get('api_version', cfg.get('api_version', '3')))
        self.ssl_verify = bool(self.config.get('ssl_verify', cfg.get('default_ssl_verify', False)))
        self.timeout = int(self.config.get('timeout', cfg.get('default_timeout', 30)))
        self.user_agent = self.config.get('user_agent', 'WorldKG-Geodata/1.0')
        self.qa_dataset_id = self.config.get('qa_dataset_id', 'catalogue-quality-scores')
        self.request_delay = float(self.config.get('request_delay_seconds', 0))
        self.download_dir = Path(cfg.get('download_dir', '/tmp/geodata_downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.bulk_batch_size = int(cfg.get('bulk_insert_batch_size', 1000))

    # ------------------------------------------------------------------
    # CKAN API plumbing
    # ------------------------------------------------------------------

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for CKAN API requests."""
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _make_request(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make a CKAN API request.

        CKAN always returns 200 OK, so the response must be validated via
        the ``success`` key per the CKAN docs.
        """
        if params is None:
            params = {}

        url = f"{self.base_url}/api/{self.api_version}/action/{action}"
        if params and action in ['package_show', 'package_list', 'resource_show']:
            query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
            url = f"{url}?{query_string}"

        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            response = urllib.request.urlopen(
                req,
                context=self._get_ssl_context(),
                timeout=self.timeout,
            )
            data = json.loads(response.read().decode("utf-8"))
            if not data.get('success', False):
                error_msg = data.get('error', {}).get('message', 'Unknown error')
                self.logger.error(f"CKAN API error for {action}: {error_msg}")
                return {}
            return data.get('result', {})
        except urllib.error.HTTPError as e:
            self.logger.error(f"HTTP error fetching {action}: {e.code} - {e.reason}")
            return {}
        except urllib.error.URLError as e:
            self.logger.error(f"URL error fetching {action}: {e.reason}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON decode error for {action}: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Unexpected error fetching {action}: {e}")
            return {}

    def _download_url(self, url: str) -> Optional[bytes]:
        """Download a raw URL with the configured SSL/timeout/User-Agent."""
        if not url:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            response = urllib.request.urlopen(
                req,
                context=self._get_ssl_context(),
                timeout=self.timeout,
            )
            return response.read()
        except Exception as e:
            self.logger.error(f"Failed to download {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # BaseSourceAdapter interface
    # ------------------------------------------------------------------

    def list_datasets(self) -> List[dict]:
        """List dataset names from the CKAN ``package_list`` action."""
        result = self._make_request('package_list')
        if isinstance(result, list):
            return [{'name': name} for name in result]
        return []

    def fetch_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """Fetch metadata for a single dataset via ``package_show``."""
        self.logger.info(f"Fetching metadata for dataset: {dataset_id}")
        return self._make_request('package_show', {'id': dataset_id})

    def download_resource(self, resource: GeoResource, force: bool = False) -> Optional[Path]:
        """
        Download a resource file to the local download dir.

        Returns the file path, or None when the file is already up to date
        (``force=False`` and ``last_downloaded_at >= last_modified``).
        """
        if not force and resource.last_downloaded_at:
            if resource.last_modified and resource.last_downloaded_at >= resource.last_modified:
                self.logger.info(f"Resource {resource.name} is up to date, skipping download")
                return None

        filename = f"{resource.source_resource_id}.{resource.format.lower()}"
        file_path = self.download_dir / filename

        content = self._download_url(resource.url)
        if content is None:
            return None

        try:
            with open(file_path, 'wb') as f:
                f.write(content)
            resource.last_downloaded_at = timezone.now()
            resource.save(update_fields=['last_downloaded_at'])
            self.logger.info(f"Downloaded {resource.name} to {file_path}")
            return file_path
        except Exception as e:
            self.logger.error(f"Failed to save {resource.name}: {e}")
            return None

    def parse_resource(self, file_path: Path, date_field: Optional[str] = None) -> Iterator[dict]:
        """Parse a downloaded CSV/JSON/GeoJSON file into record dicts.

        CSV rows become ``attributes`` with the raw columns; geometry is
        recovered from a ``geometry`` column (GeoJSON string) or lat/lon
        columns when present. GeoJSON FeatureCollections map
        ``properties`` → attributes and ``geometry`` → geom.
        """
        suffix = file_path.suffix.lower()
        if suffix == '.csv':
            yield from self._parse_csv(file_path, date_field)
        elif suffix in ('.json', '.geojson'):
            yield from self._parse_json(file_path, date_field)
        else:
            self.logger.warning(f"Unsupported file type: {suffix}")

    def _parse_csv(self, file_path: Path, date_field: Optional[str] = None) -> Iterator[dict]:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            yield from self._parse_csv_rows(f, date_field)

    def _parse_csv_rows(self, f, date_field: Optional[str] = None) -> Iterator[dict]:
        reader = csv.DictReader(f)
        for row in reader:
            yield {
                'attributes': row,
                'geom': self._geom_from_row(row),
                'source_id': str(row.get('_id', row.get('OBJECTID', row.get('id', '')))),
                'ingestion_date': self._date_from_row(row, date_field),
            }

    def _parse_json(self, file_path: Path, date_field: Optional[str] = None) -> Iterator[dict]:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Some CKAN resources are labelled GEOJSON/JSON but their
            # datastore dump endpoint returns CSV — fall back to rows.
            self.logger.warning(
                f"{file_path.name} is not valid JSON; falling back to CSV parsing"
            )
            yield from self._parse_csv_rows(io.StringIO(content), date_field)
            return

        if isinstance(data, dict) and 'features' in data:
            # GeoJSON FeatureCollection
            for feature in data.get('features', []):
                props = feature.get('properties', {})
                yield {
                    'attributes': props,
                    'geom': self._geom_from_geojson(feature.get('geometry')),
                    'source_id': str(props.get('_id', feature.get('id', ''))),
                    'ingestion_date': None,
                }
        elif isinstance(data, list):
            # Plain JSON array of objects
            for item in data:
                if not isinstance(item, dict):
                    continue
                yield {
                    'attributes': item,
                    'geom': self._geom_from_geojson(item.get('geometry')),
                    'source_id': str(item.get('_id', item.get('id', ''))),
                    'ingestion_date': self._date_from_row(item, None),
                }

    # ------------------------------------------------------------------
    # Quality tracking (generalized from CkanQaService)
    # ------------------------------------------------------------------

    def check_quality(self, dataset_id: str) -> Dict[str, Any]:
        """Fetch quality metrics for a single dataset."""
        records = self.fetch_quality_records([dataset_id])
        return records[0] if records else {}

    def fetch_quality_records(self, dataset_ids: List[str]) -> List[dict]:
        """
        Fetch quality scores for the given dataset names/slugs.

        Reads the source's QA dataset (config ``qa_dataset_id``, default
        Toronto's ``catalogue-quality-scores``), downloads its first JSON
        resource, and filters to the requested datasets.
        """
        if not dataset_ids:
            return []

        qa_pkg = self._make_request('package_show', {'id': self.qa_dataset_id})
        if not qa_pkg or 'resources' not in qa_pkg:
            self.logger.error("Failed to fetch QA dataset or no resources found")
            return []

        target_ids = set(dataset_ids)
        for resource in qa_pkg.get('resources', []):
            if resource.get('format', '').lower() != 'json':
                continue
            content = self._download_url(resource.get('url'))
            if not content:
                continue
            try:
                qa_data = json.loads(content.decode('utf-8'))
            except json.JSONDecodeError:
                continue
            return self._filter_qa_records(qa_data, target_ids)
        return []

    def _filter_qa_records(self, qa_data: List[Dict[str, Any]], target_ids: set) -> List[dict]:
        records = []
        for entry in qa_data:
            package_id = entry.get('package')
            if package_id not in target_ids:
                continue
            records.append({
                'dataset_id': package_id,
                'score': float(entry.get('score', 0)) * 100,  # convert to percentage
                'grade': entry.get('grade', 'Unknown'),
                'freshness': float(entry.get('freshness', 0)),
                'metadata_score': float(entry.get('metadata', 0)),
                'usability': float(entry.get('usability', 0)),
                'completeness': float(entry.get('completeness', 0)),
                'accessibility': float(entry.get('accessibility', 0)),
                'qa_recorded_at': entry.get('recorded_at'),
            })
        return records

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _geom_from_geojson(self, geometry: Any) -> Optional[GEOSGeometry]:
        if not geometry:
            return None
        try:
            return GEOSGeometry(json.dumps(geometry), srid=4326)
        except Exception:
            self.logger.warning(f"Failed to parse geometry: {str(geometry)[:120]}")
            return None

    def _geom_from_row(self, row: Dict[str, Any]) -> Optional[GEOSGeometry]:
        geometry_str = row.get('geometry') or row.get('Geometry')
        if geometry_str:
            try:
                return self._geom_from_geojson(json.loads(geometry_str))
            except Exception:
                pass

        lat = self._first_float(row, ['latitude', 'lat', 'LATITUDE', 'LAT'])
        lon = self._first_float(row, ['longitude', 'lon', 'LONGITUDE', 'LON'])
        if lat is not None and lon is not None:
            try:
                return Point(lon, lat, srid=4326)
            except Exception:
                return None
        return None

    def _date_from_row(self, row: Dict[str, Any], date_field: Optional[str] = None) -> Optional[date]:
        candidates = [date_field] if date_field else self.DATE_FIELD_CANDIDATES
        for key in candidates:
            if not key:
                continue
            value = row.get(key)
            if not value:
                continue
            date_str = str(value).split('T')[0]  # strip time component
            try:
                return parse_date(date_str)
            except Exception:
                continue
        return None

    def _first_float(self, row: Dict[str, Any], keys: List[str]) -> Optional[float]:
        for key in keys:
            value = row.get(key)
            if value in (None, ''):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None
