"""
Continent Snapshot Service — Phase 1 of Temporal Sharding.

Creates date-specific continent PBF snapshots from the already-extracted
flat continent PBFs (produced by Step 0.5).  Uses ``osmium time-filter``
to turn each year's worth of OSM history into a point-in-time snapshot.

Simplified architecture (single planet source):
    history-260209.osm.pbf
        ↓ Step 0.5
    continents/europe.pbf          (has full history: 2004–2025)
    continents/asia.pbf
    ...
        ↓ Step 0.6 (this service — osmium time-filter)
    continents/2025_12_31/europe.pbf
    continents/2024_12_31/europe.pbf
    continents/2023_12_31/europe.pbf
    ...
    continents/2025_12_31/asia.pbf
    continents/2024_12_31/asia.pbf
    ...

Design decisions:
    - Source is the flat continent PBFs from Step 0.5 (continents/{slug}.pbf).
    - For each continent, we create yearly snapshots at Dec 31 of each year.
    - Uses ``osmium time-filter {source} {year}-12-31T23:59:59Z -o {output}``
      — this is the same mechanism as Phase 2 of country preprocessing.
    - Idempotent: if a snapshot already exists for a date+continent, it is skipped.
    - Resumable: discovers which dates are missing and only processes those.
    - Year range: hardcoded default 2021–current year (configurable via
      START_SNAPSHOT_YEAR setting or constructor arg).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger("pipeline")

# ── Continent list ──────────────────────────────────────────────────────
# The canonical OSM continents. Slugs must match the filenames produced by
# Step 0.5 (e.g. continents/europe.pbf → slug "europe").
CONTINENTS = [
    "africa",
    "asia",
    "australia_oceania",
    "central_america",
    "europe",
    "north_america",
    "south_america",
]

# Default year range — we always snap to Dec 31 of each year.
DEFAULT_START_YEAR = 2021
DEFAULT_END_YEAR = 2025  # current target


# ══════════════════════════════════════════════════════════════════════════
# Continent Snapshot Service
# ══════════════════════════════════════════════════════════════════════════


class ContinentSnapshotService:
    """Creates date-specific continent PBFs via osmium time-filter.

    Takes the flat continent PBFs (from Step 0.5) and produces yearly
    point-in-time snapshots under ``continents/{YYYY_12_31}/{continent}.pbf``.

    The service is **resumable**: if interrupted, re-running will skip
    already-created snapshots (unless ``force=True``).
    """

    def __init__(
        self,
        output_base_dir: Optional[str] = None,
        force: bool = False,
        start_year: int = DEFAULT_START_YEAR,
        end_year: int = DEFAULT_END_YEAR,
    ):
        """
        Args:
            output_base_dir: Root for date-specific continent outputs.
                Default: ``<OSM_WIKIDATA_EXTRACTIONS_DIR>/continents``.
            force: If True, re-create even if snapshot already exists.
            start_year: First year to generate snapshots for.
            end_year: Last year to generate snapshots for.
        """
        self.output_base_dir = Path(
            output_base_dir
            or str(Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) / "continents")
        )
        self.force = force
        self.start_year = start_year
        self.end_year = end_year
        self.logger = logger

    # ── Public API ───────────────────────────────────────────────────────

    def run(self) -> Dict[str, Dict[str, Dict]]:
        """Create all yearly continent snapshots from the flat continent PBFs.

        Returns:
            Nested dict::
                {
                    "2025_12_31": {
                        "europe": { "success": True, "pbf_path": "...", ... },
                        "asia":    { "success": True, "pbf_path": "...", ... },
                        ...
                    },
                    "2024_12_31": { ... },
                    ...
                }

            Dates that were already fully complete are included with a
            ``{"status": "skipped"}`` entry.
        """
        results: Dict[str, Dict] = {}

        for snapshot_date in self._target_dates():
            self.logger.info(
                "Processing continent snapshots for %s",
                snapshot_date,
            )
            date_results = self._process_date(snapshot_date)
            results[snapshot_date] = date_results

        return results

    # ── Single-date processing ───────────────────────────────────────────

    def _process_date(self, snapshot_date: str) -> Dict:
        """Process all continents for a single snapshot date.

        Returns a dict keyed by continent slug, with values like::

            {
                "europe": {
                    "success": True,
                    "pbf_path": "/data/continents/2025_12_31/europe.pbf",
                    "file_size_bytes": 1234567890,
                    "duration_seconds": 123.4,
                },
                ...
            }

        Or if the entire date is already complete::

            {"status": "skipped", "message": "Already complete — 6/6 continents exist"}
        """
        output_dir = self.output_base_dir / snapshot_date

        # Quick skip: if ALL continents already exist, skip the date entirely
        if not self.force and self._is_date_complete(snapshot_date):
            existing = len(list(output_dir.glob("*.pbf")))
            self.logger.info(
                "Snapshot %s already complete (%d/%d continents) — skipping",
                snapshot_date,
                existing,
                len(CONTINENTS),
            )
            return {
                "status": "skipped",
                "message": f"Already complete — {existing}/{len(CONTINENTS)} continents exist",
            }

        output_dir.mkdir(parents=True, exist_ok=True)

        date_results: Dict = {}

        for continent_slug in CONTINENTS:
            result = self._create_single_snapshot(
                continent_slug=continent_slug,
                snapshot_date=snapshot_date,
                output_dir=output_dir,
            )
            date_results[continent_slug] = result

        # Summary
        success_count = sum(
            1 for r in date_results.values()
            if isinstance(r, dict) and r.get("success")
        )
        self.logger.info(
            "Snapshot %s: %d/%d continents done",
            snapshot_date,
            success_count,
            len(CONTINENTS),
        )

        return date_results

    def _create_single_snapshot(
        self,
        continent_slug: str,
        snapshot_date: str,
        output_dir: Path,
    ) -> Dict:
        """Create a single continent snapshot using ``osmium time-filter``.

        The source PBF is the flat continent PBF at
        ``{output_base_dir}/{continent}.pbf``.

        For example::

            /data/continents/europe.pbf              ← flat (full history)
            /data/continents/2025_12_31/             ← date-specific subdir
            /data/continents/2024_12_31/

        So we derive the source by going up one level from the date subdir.
        """
        output_pbf_path = output_dir / f"{continent_slug}.pbf"

        # Skip if already exists (idempotent)
        if output_pbf_path.exists() and not self.force:
            return {
                "success": True,
                "pbf_path": str(output_pbf_path),
                "file_size_bytes": output_pbf_path.stat().st_size,
                "status": "already_exists",
            }

        # Source is the flat continent PBF, one level up
        source_pbf = output_dir.parent / f"{continent_slug}.pbf"
        if not source_pbf.exists():
            # Try alternative slug (hyphen instead of underscore, or vice versa)
            alt_slug = continent_slug.replace("_", "-")
            source_pbf = output_dir.parent / f"{alt_slug}.pbf"
        if not source_pbf.exists():
            self.logger.error(
                "Cannot create snapshot %s/%s: source PBF not found at %s",
                snapshot_date,
                continent_slug,
                source_pbf,
            )
            return {
                "success": False,
                "error": f"Source continent PBF not found: {source_pbf}",
            }

        # Parse the year from the snapshot date (e.g. "2025_12_31" → 2025)
        try:
            year = int(snapshot_date.split("_")[0])
        except (IndexError, ValueError):
            return {
                "success": False,
                "error": f"Cannot parse year from snapshot date: {snapshot_date}",
            }

        # Build the timestamp for osmium time-filter: YYYY-12-31T23:59:59Z
        timestamp = f"{year}-12-31T23:59:59Z"

        self.logger.info(
            "Creating snapshot %s for %s from %s (time-filter @ %s)",
            snapshot_date,
            continent_slug,
            source_pbf.name,
            timestamp,
        )

        t0 = time.monotonic()

        try:
            from extraction.services.osmium_facade import OsmiumFacade

            osmium = OsmiumFacade()
            result = osmium.time_filter(
                input_file=str(source_pbf),
                timestamp=timestamp,
                output_file=str(output_pbf_path),
            )

            duration = time.monotonic() - t0

            if result.get("success"):
                file_size = (
                    output_pbf_path.stat().st_size
                    if output_pbf_path.exists()
                    else 0
                )
                self.logger.info(
                    "  ✓ %s → %s (%.1f MB in %.1fs)",
                    continent_slug,
                    output_pbf_path.name,
                    file_size / (1024 * 1024),
                    duration,
                )

                # Register in DB
                self._register_snapshot(
                    continent_slug=continent_slug,
                    snapshot_date=snapshot_date,
                    pbf_path=str(output_pbf_path),
                    source_pbf_path=str(source_pbf),
                    file_size=file_size,
                    duration=duration,
                )

                return {
                    "success": True,
                    "pbf_path": str(output_pbf_path),
                    "file_size_bytes": file_size,
                    "duration_seconds": round(duration, 1),
                    "timestamp": timestamp,
                }
            else:
                self.logger.error(
                    "  ✗ %s: osmium time-filter failed: %s",
                    continent_slug,
                    result.get("error"),
                )
                return {
                    "success": False,
                    "error": result.get("error", "osmium time-filter failed"),
                    "duration_seconds": round(duration, 1),
                }

        except Exception as e:
            duration = time.monotonic() - t0
            self.logger.error(
                "  ✗ %s: unexpected error: %s",
                continent_slug,
                e,
            )
            return {
                "success": False,
                "error": str(e),
                "duration_seconds": round(duration, 1),
            }

    # ── DB registration ──────────────────────────────────────────────────

    def _register_snapshot(
        self,
        continent_slug: str,
        snapshot_date: str,
        pbf_path: str,
        source_pbf_path: str,
        file_size: int,
        duration: float,
    ) -> None:
        """Register the newly created snapshot PBF in the database."""
        try:
            from api.models import PbfFile, PbfExtract
            from django.utils import timezone as tz

            # Find or create the source PbfFile record
            source_pbf, _ = PbfFile.objects.get_or_create(
                path=source_pbf_path,
                defaults={
                    "pbf_file_type": PbfFile.PbfType.CONTINENT,
                    "extraction_level": PbfFile.ExtractionLevel.CONTINENT,
                    "has_history": True,
                    "size_bytes": (
                        Path(source_pbf_path).stat().st_size
                        if Path(source_pbf_path).exists()
                        else 0
                    ),
                },
            )

            # Create or update snapshot PbfFile
            snapshot_pbf, _ = PbfFile.objects.update_or_create(
                path=pbf_path,
                defaults={
                    "pbf_file_type": PbfFile.PbfType.CONTINENT,
                    "extraction_level": PbfFile.ExtractionLevel.SNAPSHOT,
                    "parent_pbf": source_pbf,
                    "has_history": False,  # point-in-time snapshot
                    "size_bytes": file_size,
                    "status": PbfFile.PbfStatus.COMPLETED,
                },
            )

            # Create PbfExtract record
            PbfExtract.objects.create(
                source_pbf=source_pbf,
                output_pbf_path=pbf_path,
                source_file_size_bytes=(
                    source_pbf.size_bytes or 0
                ),
                output_file_size_bytes=file_size,
                duration_seconds=duration,
                start_time=tz.now(),
                end_time=tz.now(),
            )

            self.logger.info(
                "Registered snapshot PBF: %s (id=%s)",
                pbf_path,
                snapshot_pbf.id,
            )

        except Exception as e:
            self.logger.warning(
                "DB registration skipped for %s: %s",
                pbf_path,
                e,
            )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _target_dates(self) -> List[str]:
        """Return sorted list of target snapshot dates (newest first).

        E.g. ["2025_12_31", "2024_12_31", ..., "2021_12_31"].
        """
        dates = [
            f"{year}_12_31"
            for year in range(self.start_year, self.end_year + 1)
        ]
        return sorted(dates, reverse=True)

    def _is_date_complete(self, snapshot_date: str) -> bool:
        """Check if all known continent PBFs exist for a date.

        A date is complete if its directory exists and contains at least
        one .pbf file. We don't require ALL continents because some
        (e.g. antarctica) may not have been extracted.
        """
        output_dir = self.output_base_dir / snapshot_date
        if not output_dir.exists():
            return False
        pbf_files = list(output_dir.glob("*.pbf"))
        return len(pbf_files) > 0

    # ── Static path helpers ──────────────────────────────────────────────

    @staticmethod
    def get_continent_pbf_path(
        continent_slug: str,
        snapshot_date: str = "2025_12_31",
        base_dir: Optional[str] = None,
    ) -> Path:
        """Get the expected path for a continent PBF at a given snapshot date.

        Example:
            /data/continents/2025_12_31/europe.pbf
        """
        base = Path(
            base_dir
            or getattr(settings, "OSM_WIKIDATA_EXTRACTIONS_DIR", "/data")
        )
        return base / "continents" / snapshot_date / f"{continent_slug}.pbf"

    @staticmethod
    def verify_continent_pbf(
        continent_slug: str,
        snapshot_date: str = "2025_12_31",
    ) -> Tuple[bool, Optional[Path], Optional[int]]:
        """Verify that a continent snapshot PBF exists and return its size.

        Returns:
            Tuple of (exists, path, file_size_bytes_or_None).
        """
        path = ContinentSnapshotService.get_continent_pbf_path(
            continent_slug, snapshot_date
        )
        if path.exists():
            return True, path, path.stat().st_size
        return False, path, None
