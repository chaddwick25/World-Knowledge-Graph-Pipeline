"""Shared service for building subgraph lists for USLP and other subgraph-level operations.

This module provides a unified way to resolve and normalize subgraph lists,
supporting both auto-discovery (all subgraphs for a country) and manual selection
(frontend-provided subgraph lists).
"""

from typing import List, Dict, Optional
from pathlib import Path
import logging

from core.services.planet_init.osm_wikidata_resolver import get_country_by_name
from core.services.snapshot.regional_path_service import (
    normalize_country_slug,
    normalize_continent_slug,
    regional_path_service,
)

logger = logging.getLogger(__name__)


def build_subgraph_list(
    country_name: str,
    auto_all: bool = True,
    selected: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Build a normalized list of subgraphs for USLP processing.

    Args:
        country_name: Human-readable country name (e.g., "Tanzania", "Belize").
        auto_all: If True, auto-discover all subgraphs from filesystem.
                  If False, validate and normalize the `selected` list.
        selected: Optional list of subgraph dicts from frontend.
                  Each dict should have at least `poly_path` and optionally `name`, `slug`.

    Returns:
        List of normalized subgraph dicts with keys: `name`, `slug`, `poly_path`.

    Raises:
        ValueError: If country metadata cannot be resolved or no valid subgraphs found.
    """
    # Resolve country metadata
    country_data = get_country_by_name(country_name)
    if not country_data:
        raise ValueError(f"Could not resolve country metadata for {country_name}")

    # Use continent_name if available, otherwise fall back to parent_slug
    continent_raw = country_data.get('continent_name') or country_data.get('parent_slug') or ''
    continent = normalize_continent_slug(continent_raw) if continent_raw else 'unknown'
    raw_slug = country_data.get('slug') or country_name
    country_slug = normalize_country_slug(raw_slug)

    # Use centralized service to resolve the correct directory slug
    from core.services.snapshot.regional_path_service import regional_path_service
    country_slug = regional_path_service.resolve_country_slug_for_subgraphs(
        continent, country_slug, country_name, country_data
    )

    # Get subgraphs directory
    subgraphs_root = regional_path_service.get_subgraphs_dir(continent, country_slug)

    if auto_all:
        # Auto-discover all subgraphs from filesystem
        if not subgraphs_root.exists():
            logger.warning(f"No subgraphs directory found for {country_name}: {subgraphs_root}")
            return []

        subgraphs = []
        for sg_dir in sorted(p for p in subgraphs_root.iterdir() if p.is_dir()):
            slug = sg_dir.name
            name = slug.replace('_', ' ')
            poly_files = sorted(sg_dir.glob('*.osm.poly'))
            poly_path = str(poly_files[0]) if poly_files else None
            if not poly_path:
                logger.warning(f"Subgraph {slug} has no poly file, skipping")
                continue
            subgraphs.append({
                'name': name,
                'slug': slug,
                'poly_path': poly_path,
            })

        if not subgraphs:
            logger.warning(f"No subgraphs with poly files found for {country_name}")
            return []

        logger.info(f"Auto-discovered {len(subgraphs)} subgraphs for {country_name}")
        return subgraphs

    else:
        # Manual selection: validate and normalize frontend-provided list
        if not selected:
            raise ValueError("Manual selection mode requires 'selected' subgraph list")

        subgraphs = []
        for sg in selected:
            poly_path = sg.get('poly_path')
            if not poly_path:
                logger.warning(f"Subgraph entry missing poly_path, skipping: {sg}")
                continue

            # Validate poly file exists
            poly_path_obj = Path(poly_path)
            if not poly_path_obj.exists():
                logger.warning(f"Poly file not found: {poly_path}, skipping")
                continue

            # Normalize name and slug
            name = sg.get('name') or sg.get('slug')
            slug = sg.get('slug') or (sg.get('name') or '').lower().replace(' ', '_')

            if not name:
                # Derive name from poly file if missing
                name = slug.replace('_', ' ')

            subgraphs.append({
                'name': name,
                'slug': slug,
                'poly_path': str(poly_path_obj),
            })

        if not subgraphs:
            raise ValueError(
                f"No valid subgraphs found in provided selection for {country_name}"
            )

        logger.info(f"Validated {len(subgraphs)} manually-selected subgraphs for {country_name}")
        return subgraphs
