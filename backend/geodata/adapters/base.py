from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class BaseSourceAdapter:
    """Common interface for all source adapters.

    Each adapter knows how to talk to one source protocol (CKAN, WFS,
    Socrata, direct download, REST) and translates it into the generic
    record shape consumed by :class:`geodata.services.ingestion_service`.

    A parsed record is a dict with the following keys::

        {
            'attributes': dict,        # the actual row data (flexible schema)
            'geom': GEOSGeometry|None, # geometry in EPSG:4326 when derivable
            'source_id': str,          # row ID from the source
            'ingestion_date': date|None,
        }
    """

    def __init__(self, source):
        self.source = source
        self.config = source.config or {}

    def list_datasets(self) -> List[dict]:
        """List available datasets from this source."""
        raise NotImplementedError

    def fetch_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """Fetch metadata for a single dataset."""
        raise NotImplementedError

    def download_resource(self, resource, force: bool = False) -> Optional[Path]:
        """Download a resource file. Returns the path, or None if up-to-date."""
        raise NotImplementedError

    def parse_resource(self, file_path: Path, date_field: Optional[str] = None) -> Iterator[dict]:
        """Parse a downloaded file into record dicts (see class docstring)."""
        raise NotImplementedError

    def check_quality(self, dataset_id: str) -> Dict[str, Any]:
        """Fetch quality metrics for a single dataset."""
        raise NotImplementedError
