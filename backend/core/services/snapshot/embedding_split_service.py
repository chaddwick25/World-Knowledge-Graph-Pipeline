"""Stateless service: split multi-country GeoVectors TSVs.

Wraps the shapely spatial splitter with RAM-disk osmium extraction for
50-100x I/O speedup. Handles GB (great-britain → united-kingdom) and
MY/SG/BN (malaysia-singapore-brunei) splits.

Idempotent: skips if all output TSVs already exist.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List

from django.conf import settings

logger = logging.getLogger(__name__)

# Map continent → source PBF name
_CONTINENT_TO_PBF = {
    "europe": "europe.pbf",
    "asia": "asia.pbf",
}

# Map continent_pbf slug → region polygon path (relative to POLYGON_FILES_DIR)
_REGION_POLY_MAP = {
    "great-britain": "europe/united_kingdom.poly",
    "malaysia-singapore-brunei": "asia/malaysia_singapore_brunei.poly",
}


class EmbeddingSplitService:
    """Split multi-country TSVs using osmium extract + shapely spatial splitter.

    Stateless — takes paths in ``__init__`` (dependency injection).
    """

    def __init__(
        self,
        embeddings_root: Path,
        continents_root: Path,
        polygons_root: Path,
    ) -> None:
        self.embeddings_root = Path(embeddings_root)
        self.continents_root = Path(continents_root)
        self.polygons_root = Path(polygons_root)

    def run(self) -> Dict[str, List[str]]:
        """Execute the split.

        Returns:
            ``{"split": [...], "skipped": [...], "failed": [...]}`` —
            labels of the split groups in each category.
        """
        from core.management.commands.preprocess_embeddings import (
            load_embedding_splits_config,
        )
        from core.services.snapshot.embedding_spatial_split_service import (
            EmbeddingSpatialSplitService,
        )

        split_config = load_embedding_splits_config()
        splits = split_config.get("splits", [])

        # Check if all output TSVs already exist — skip if so.
        # Output path pattern: embeddings_root / continent / target_slug / output_filename
        # (must match the pattern used by EmbeddingSpatialSplitService)
        all_outputs_exist = True
        for split_def in splits:
            continent = split_def.get("continent", "")
            for t in split_def.get("targets", []):
                output = t.get("output")
                slug = t.get("slug", "")
                if not output or not slug:
                    continue
                output_path = self.embeddings_root / continent / slug / output
                if not output_path.exists():
                    all_outputs_exist = False
                    logger.info(
                        "Output TSV missing: %s — will run split",
                        output_path,
                    )
                    break
            if not all_outputs_exist:
                break

        if all_outputs_exist:
            logger.info("All split output TSVs already exist; skipping split step")
            return {"split": [], "skipped": [d.get("continent_pbf", "?") for d in splits], "failed": []}

        ram_continents = Path("/dev/shm/continents")
        ram_continents.mkdir(parents=True, exist_ok=True)

        split: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        extracted_pbfs: List[Path] = []

        try:
            for split_def in splits:
                continent = split_def.get("continent")
                continent_pbf = split_def.get("continent_pbf")
                targets = split_def.get("targets", [])
                if not continent or not continent_pbf or not targets:
                    continue

                if continent not in _CONTINENT_TO_PBF:
                    logger.warning("Unknown continent: %s", continent)
                    skipped.append(continent_pbf)
                    continue

                # Use snapshot PBF (regular, not history) for extraction
                src_pbf = self.continents_root / f"{continent}_snapshot.pbf"
                if not src_pbf.exists():
                    logger.warning("Continent snapshot PBF not found: %s", src_pbf)
                    skipped.append(continent_pbf)
                    continue

                region_poly_name = _REGION_POLY_MAP.get(continent_pbf)
                if not region_poly_name:
                    logger.warning("No region polygon mapping for %s", continent_pbf)
                    skipped.append(continent_pbf)
                    continue

                poly_path = EmbeddingSpatialSplitService.resolve_poly_path(
                    self.polygons_root, region_poly_name,
                )
                if not poly_path or not poly_path.exists():
                    logger.warning("Region poly file not found: %s", region_poly_name)
                    skipped.append(continent_pbf)
                    continue

                # Extract once using the region polygon to RAM disk
                ram_pbf = ram_continents / f"{continent_pbf}.pbf"
                if ram_pbf.exists():
                    logger.info("Region PBF already in RAM: %s", ram_pbf.name)
                else:
                    logger.info("Extracting %s region using %s...", continent_pbf, region_poly_name)
                    result = subprocess.run(
                        [
                            "osmium", "extract",
                            "--overwrite",
                            "-p", str(poly_path),
                            "-o", str(ram_pbf),
                            str(src_pbf),
                        ],
                        capture_output=True, text=True, timeout=600,
                    )
                    if result.returncode != 0:
                        logger.error("osmium extract failed for %s: %s", continent_pbf, result.stderr)
                        failed.append(continent_pbf)
                        continue
                    logger.info("Extracted %.2f GB to RAM: %s",
                                ram_pbf.stat().st_size / 1e9, ram_pbf.name)

                extracted_pbfs.append(ram_pbf)
                split.append(continent_pbf)

            # Run the shapely spatial splitter on the RAM extracts
            from django.core.management import call_command
            call_command(
                "preprocess_embeddings",
                backend="shapely",
                continents_root=str(ram_continents),
            )
        finally:
            # Cleanup RAM disk extracts
            for ram_pbf in extracted_pbfs:
                try:
                    if ram_pbf.exists():
                        ram_pbf.unlink()
                        logger.info("Cleaned up %s from RAM disk", ram_pbf.name)
                except Exception as e:
                    logger.warning("Failed to cleanup %s: %s", ram_pbf, e)

        return {"split": split, "skipped": skipped, "failed": failed}
