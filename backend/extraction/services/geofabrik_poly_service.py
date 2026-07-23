import logging
import shutil
from pathlib import Path
from typing import Dict, Optional

import requests
from django.conf import settings

from extraction.services.geofabrik_index_service import geofabrik_index_service


logger = logging.getLogger(__name__)


def _build_path(node_id: str, regions_dict: Dict[str, dict]) -> list:
    """Recursively builds the path list like ['europe', 'germany', 'bavaria']."""
    if node_id not in regions_dict:
        return [node_id]

    node = regions_dict[node_id]
    parent_id = node.get("parent")

    if parent_id:
        return _build_path(parent_id, regions_dict) + [node_id]
    return [node_id]


def _get_full_id(node_id: str, regions_dict: Dict[str, dict]) -> str:
    """Returns a slash-joined id like 'europe/germany/bavaria'."""
    path_parts = _build_path(node_id, regions_dict)
    return "/".join(path_parts)


def download_all_geofabrik_polygons(
    base_dir: Optional[str] = None,
    index_url: Optional[str] = None,
    timeout: int = 10,
    overlay_dir: Optional[str] = None,
) -> Dict[str, int]:
    """Download all available Geofabrik .poly files into the given base directory.

    Returns a stats dict with counts for downloaded, skipped, not_found, and errors.
    """

    if base_dir is None:
        base_dir = getattr(settings, "POLYGON_FILES_DIR", None)

    if not base_dir:
        raise ValueError("POLYGON_FILES_DIR is not configured.")

    base_path = Path(base_dir)

    if index_url is None:
        index_url = getattr(settings, "GEOFABRIK_INDEX_URL", None) or geofabrik_index_service.INDEX_URL

    # Use the cached Geofabrik index (data/geofabrik_index.json) when available
    # to ensure a stable, reproducible polygon tree that matches previous runs.
    logger.info("Loading Geofabrik index (cache preferred) from %s...", index_url)
    data = geofabrik_index_service.fetch_index(force_refresh=False)

    features = data.get("features", [])
    regions_dict: Dict[str, dict] = {}
    for feature in features:
        props = feature.get("properties") or {}
        node_id = props.get("id")
        if node_id:
            regions_dict[node_id] = props

    logger.info("Geofabrik index contains %d regions.", len(regions_dict))

    downloaded = 0
    skipped = 0
    not_found = 0
    errors = 0

    for node_id in regions_dict.keys():
        full_id = _get_full_id(node_id, regions_dict)

        # Geofabrik URL uses the full path like africa/algeria.poly
        poly_url = f"https://download.geofabrik.de/{full_id}.poly"

        # Replace hyphens with underscores for local filesystem naming.
        # This ensures continent poly files (e.g. north-america → north_america)
        # match the DB convention (underscores everywhere).
        sanitized_full_id = full_id.replace("-", "_")
        local_path = base_path / f"{sanitized_full_id}.poly"

        if local_path.exists():
            skipped += 1
            continue

        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            resp = requests.get(poly_url, timeout=timeout)

            if resp.status_code == 200:
                with local_path.open("wb") as f:
                    f.write(resp.content)
                downloaded += 1
                logger.info("Downloaded polygon: %s", full_id)
            elif resp.status_code == 404:
                not_found += 1
                logger.debug("Polygon not found (404): %s", full_id)
            else:
                errors += 1
                logger.warning(
                    "Failed to download polygon %s: HTTP %s", full_id, resp.status_code
                )
        except Exception as exc:
            errors += 1
            logger.error("Error downloading polygon %s: %s", full_id, exc)

    logger.info(
        "Geofabrik polygon download complete: %d downloaded, %d skipped, %d not found, %d errors",
        downloaded,
        skipped,
        not_found,
        errors,
    )

    # Optionally overlay legacy/custom polygons (e.g., africa/mozambique/*.poly)
    if overlay_dir is None:
        overlay_dir = getattr(settings, "LEGACY_POLYGON_FILES_DIR", None)

    if overlay_dir:
        legacy_path = Path(overlay_dir)
        if legacy_path.exists():
            extra = 0
            try:
                for src in legacy_path.rglob("*.poly"):
                    rel = src.relative_to(legacy_path)
                    dst = base_path / rel
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        extra += 1
            except Exception as exc:
                logger.error(
                    "Failed to overlay legacy polygons from %s: %s", legacy_path, exc
                )
            else:
                if extra:
                    logger.info(
                        "Overlayed %d legacy polygons from %s into %s",
                        extra,
                        str(legacy_path),
                        str(base_path),
                    )

    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "not_found": not_found,
        "errors": errors,
    }

