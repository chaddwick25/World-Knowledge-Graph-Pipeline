"""SnapshotExtractionService — Country snapshot extraction from continent PBF.

Replaces ``TemporalOrchestratorService`` (LEGACY). Single responsibility:
extract a country snapshot PBF from the **continent** PBF (already extracted
during planet init) for a given snapshot date, using a ``.poly`` boundary
file and ``osmium time-filter``.

Hierarchy (see docs/plans/TEMPORAL_SNAPSHOT_REFACTOR.md Phase B):

    planet PBF ──osmium extract──▶ continent PBF (with history)
                                           │
                                  osmium extract -p {country.poly}
                                           ▼
                                   country extract (with history)
                                           │
                                  osmium time-filter {date}
                                           ▼
                                   country snapshot PBF

Flow:

  1. Resolve the continent PBF (from ``OSM_CONTINENTS_OUTPUT_DIR``).
  2. Resolve the poly file (caller-supplied → ``OsmBoundary`` → on-disk →
     generate from planet PBF via ``osmium getid``).
  3. ``osmium extract -p {poly} {continent_pbf}`` → country extract (with
     history).
  4. ``osmium time-filter {extract} {YYYY}-12-31T23:59:59Z`` → snapshot PBF.
  5. Validate output (exists, non-zero, ``osmium fileinfo`` succeeds).
  6. Register a ``PbfFile`` row for the snapshot.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from django.conf import settings

from .osmium_facade import OsmiumFacade
from .regional_path_service import (
    regional_path_service,
    normalize_continent_slug,
    normalize_country_slug,
)
from .country_override_service import get_country_slug

logger = logging.getLogger(__name__)


class SnapshotExtractionService:
    """Extract a country snapshot PBF from the continent PBF.

    Stateless — no constructor args. One method, ``extract_country_snapshot``,
    returns a result dict consumed by ``preprocess_snapshot()`` in
    ``pipeline/tasks/helper.py``.
    """

    def __init__(self) -> None:
        self.osmium = OsmiumFacade()

    # ── Public API ───────────────────────────────────────────────────────
    def extract_country_snapshot(
        self,
        country_code: str,
        country_name: str,
        continent: str,
        snapshot_date: str,
        osm_relation_id: Optional[int] = None,
        poly_file_path: Optional[str] = None,
        country_slug: Optional[str] = None,
    ) -> dict:
        """Extract a country snapshot PBF from the continent PBF.

        Args:
            country_code: ISO 3166-1 alpha-2 code (e.g., "JM").
            country_name: Human-readable country name (e.g., "Jamaica").
            continent: Continent slug/name (e.g., "north-america").
            snapshot_date: ``YYYY_MM_DD`` format (e.g., "2025_12_31").
            osm_relation_id: OSM relation ID for the country boundary.
                Required when ``poly_file_path`` is not supplied and the
                poly file must be generated from the planet PBF.
            poly_file_path: Optional pre-resolved ``.poly`` file path.
                If supplied, skips ``OsmBoundary`` lookup.
            country_slug: Optional pre-resolved country slug (the
                ``CountryPipelineProfile.canonical_slug`` gate, e.g.
                ``"netherlands"`` for NL). When supplied, it REPLACES the
                ``normalize_country_slug(country_name)`` derivation and is
                used as the base for ``get_country_slug`` (overrides.json
                still applies on top). Without it, the legacy derivation
                from ``country_name`` is kept for backward compatibility.

        Returns:
            Dict with keys: ``success``, ``snapshot_pbf_path``,
            ``poly_file_path``, ``entity_count``, ``error``.
        """
        if not snapshot_date:
            return self._fail("snapshot_date is required")

        planet_pbf = getattr(settings, "PLANET_OSM_FILE_PATH", None)
        cont_norm = normalize_continent_slug(continent)
        default_slug = country_slug or normalize_country_slug(country_name)
        resolved_slug = (
            get_country_slug(country_code, default_slug)
            if country_code
            else default_slug
        )

        # ── 1. Resolve continent PBF (source for extraction) ──────────
        continent_pbf = self._resolve_continent_pbf(cont_norm)
        if not continent_pbf:
            return self._fail(
                f"Continent PBF not found for {cont_norm!r}. "
                f"Ensure planet init has extracted continent PBFs."
            )

        # ── 2. Resolve output paths ────────────────────────────────────
        snapshot_pbf_path = regional_path_service.get_single_snapshot_pbf_path(
            cont_norm, resolved_slug, snapshot_date,
        )

        # ── 3. Resolve poly file ───────────────────────────────────────
        resolved_poly = self._resolve_poly_file(
            poly_file_path=poly_file_path,
            country_code=country_code,
            continent=cont_norm,
            country_slug=resolved_slug,
            osm_relation_id=osm_relation_id,
            planet_pbf=planet_pbf,
        )
        if not resolved_poly:
            return self._fail(
                f"Could not resolve a .poly file for {country_code} "
                f"(relation_id={osm_relation_id})"
            )

        # ── 4. Short-circuit if snapshot already exists ────────────────
        if Path(snapshot_pbf_path).exists() and Path(snapshot_pbf_path).stat().st_size > 0:
            logger.info(
                "Snapshot PBF already exists — skipping extraction [country=%s path=%s]",
                country_code, snapshot_pbf_path,
            )
            return {
                "success": True,
                "snapshot_pbf_path": str(snapshot_pbf_path),
                "poly_file_path": str(resolved_poly),
                "entity_count": 0,
                "error": None,
                "skipped": True,
            }

        # ── 5. Extract from continent PBF (with history) ───────────────
        Path(snapshot_pbf_path).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".osm.pbf", delete=False, dir=str(Path(snapshot_pbf_path).parent),
        ) as tmp:
            extract_pbf = tmp.name

        try:
            extract_ok = self._osmium_extract_polygon(
                str(continent_pbf), str(resolved_poly), extract_pbf,
            )
            if not extract_ok:
                return self._fail(
                    f"osmium extract --polygon failed for {country_code} "
                    f"(continent_pbf={continent_pbf})"
                )

            # ── 6. Time-filter to snapshot date ────────────────────────
            syear, smonth, sday = (int(x) for x in snapshot_date.split("_"))
            timestamp = f"{syear}-{smonth:02d}-{sday:02d}T23:59:59Z"
            time_ok = self._osmium_time_filter(
                extract_pbf, timestamp, str(snapshot_pbf_path),
            )
            if not time_ok:
                return self._fail(
                    f"osmium time-filter failed for {country_code} @ {snapshot_date}"
                )

            # ── 7. Validate output ─────────────────────────────────────
            if not Path(snapshot_pbf_path).exists() or Path(snapshot_pbf_path).stat().st_size == 0:
                return self._fail(
                    f"Snapshot PBF missing or empty: {snapshot_pbf_path}"
                )
            info = self.osmium.file_info(str(snapshot_pbf_path))
            if info.get("exit_code", 1) != 0:
                return self._fail(
                    f"osmium fileinfo failed for {snapshot_pbf_path}: {info.get('error')}"
                )
            entity_count = self._extract_entity_count(info)

            # ── 8. Register PbfFile row ────────────────────────────────
            pbf_file = self._register_pbf_file(
                snapshot_pbf_path=str(snapshot_pbf_path),
                source_pbf_path=str(continent_pbf),
                entity_count=entity_count,
            )

            # ── 9. Register Snapshot row ───────────────────────────────
            self._register_snapshot(
                country_code=country_code,
                snapshot_date=snapshot_date,
                pbf_file=pbf_file,
            )

            logger.info(
                "Snapshot extraction complete [country=%s date=%s entities=%s path=%s]",
                country_code, snapshot_date, entity_count, snapshot_pbf_path,
            )
            return {
                "success": True,
                "snapshot_pbf_path": str(snapshot_pbf_path),
                "poly_file_path": str(resolved_poly),
                "entity_count": entity_count,
                "error": None,
            }
        finally:
            try:
                if os.path.exists(extract_pbf):
                    os.unlink(extract_pbf)
            except OSError:
                pass

    # ── Helpers ──────────────────────────────────────────────────────────
    def _resolve_continent_pbf(self, continent_slug: str) -> Optional[Path]:
        """Resolve the continent PBF (with history) path.

        Continent PBFs are created by planet init at:
            ``{OSM_CONTINENTS_OUTPUT_DIR}/{continent_slug}.pbf``

        Falls back to ``regional_path_service.get_continent_pbf_path()`` if
        the continents directory doesn't have it.
        """
        continents_dir = getattr(settings, "OSM_CONTINENTS_OUTPUT_DIR", None)
        if continents_dir:
            pbf = Path(continents_dir) / f"{continent_slug}.pbf"
            if pbf.exists() and pbf.stat().st_size > 0:
                return pbf

        # Fallback: regional_path_service convention
        fallback = regional_path_service.get_continent_pbf_path(continent_slug)
        if fallback.exists() and fallback.stat().st_size > 0:
            return fallback

        return None

    def _resolve_poly_file(
        self,
        poly_file_path: Optional[str],
        country_code: str,
        continent: str,
        country_slug: str,
        osm_relation_id: Optional[int],
        planet_pbf: str,
    ) -> Optional[Path]:
        """Resolve a .poly file: caller-supplied → OsmBoundary → on-disk → generate."""
        # 1. Caller-supplied
        if poly_file_path and Path(poly_file_path).exists():
            return Path(poly_file_path)

        # 2. OsmBoundary DB lookup (polygon_file FK → PolygonFile.file_path)
        try:
            from core.models import OsmBoundary
            boundary = (
                OsmBoundary.objects.filter(iso_code__iexact=country_code, admin_level=2)
                .select_related("polygon_file")
                .first()
            )
            if boundary and boundary.polygon_file_id:
                pf = boundary.polygon_file
                if pf and pf.file_path and Path(pf.file_path).exists():
                    return Path(pf.file_path)
        except Exception as exc:
            logger.warning("OsmBoundary poly lookup failed: %s", exc)

        # 3. On-disk fallback (regional_path_service convention)
        candidate = (
            Path(settings.BASE_DATA_DIR)
            / "OSM-PBF-FILES"
            / "osm_polygon_files"
            / continent
            / f"{country_slug}.poly"
        )
        if candidate.exists():
            return candidate

        # 4. Generate from planet PBF via osmium getid + export
        if osm_relation_id and planet_pbf:
            from .pbf_bounding_box_service import pbf_bounding_box_service
            generated = (
                Path(settings.BASE_DATA_DIR)
                / "OSM-PBF-FILES"
                / "osm_polygon_files"
                / continent
                / f"{country_slug}.poly"
            )
            generated.parent.mkdir(parents=True, exist_ok=True)
            ok = pbf_bounding_box_service.generate_high_res_poly(
                planet_pbf, osm_relation_id, str(generated),
            )
            if ok and generated.exists():
                return generated

        return None

    def _osmium_extract_polygon(
        self, source_pbf: str, poly_file: str, output_pbf: str,
    ) -> bool:
        """Run ``osmium extract -p {poly} {source_pbf}`` to extract a country."""
        osmium_path = getattr(settings, "OSMIUM_BINARY_PATH", "osmium")
        cmd = [
            osmium_path, "extract",
            "--with-history",
            "--strategy=complete_ways",
            "--overwrite",
            "-p", poly_file,
            "-o", output_pbf,
            source_pbf,
        ]
        logger.info("osmium extract --polygon: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("osmium extract failed: %s", exc)
            return False
        if proc.returncode != 0:
            logger.error(
                "osmium extract returned %s — stderr: %s",
                proc.returncode, proc.stderr[:2000] if proc.stderr else "(empty)",
            )
            return False
        return Path(output_pbf).exists() and Path(output_pbf).stat().st_size > 0

    def _osmium_time_filter(
        self, source_pbf: str, timestamp: str, output_pbf: str,
    ) -> bool:
        """Run ``osmium time-filter`` to flatten history to a point in time."""
        result = self.osmium.time_filter(
            input_file=source_pbf,
            timestamp=timestamp,
            output_file=output_pbf,
        )
        if not result.get("success"):
            logger.error("osmium time-filter failed: %s", result.get("error"))
            return False
        return Path(output_pbf).exists() and Path(output_pbf).stat().st_size > 0

    def _extract_entity_count(self, file_info: dict) -> int:
        """Best-effort node+way+relation count from osmium fileinfo JSON."""
        try:
            info = file_info.get("info", {})
            objects = info.get("objects", {})
            n = int(objects.get("nodes", 0))
            w = int(objects.get("ways", 0))
            r = int(objects.get("relations", 0))
            return n + w + r
        except Exception:
            return 0

    def _register_pbf_file(
        self, snapshot_pbf_path: str, source_pbf_path: str, entity_count: int,
    ):
        """Create/update a PbfFile row for the snapshot. Never raises.

        Returns the PbfFile instance (or None on failure).
        """
        try:
            from core.models import PbfFile
            defaults = {
                "pbf_file_type": PbfFile.PbfType.LATEST,
                "extraction_level": PbfFile.ExtractionLevel.SNAPSHOT,
                "status": PbfFile.PbfStatus.COMPLETED,
                "size_bytes": Path(snapshot_pbf_path).stat().st_size,
            }
            pbf_file, _ = PbfFile.objects.update_or_create(
                path=snapshot_pbf_path, defaults=defaults,
            )
            return pbf_file
        except Exception as exc:
            logger.warning("PbfFile registration failed (non-fatal): %s", exc)
            return None

    def _register_snapshot(
        self, country_code: str, snapshot_date: str, pbf_file,
    ) -> None:
        """Create/update a Snapshot row. Never raises."""
        if pbf_file is None:
            return
        try:
            from osmsnapshot.models import Snapshot
            Snapshot.objects.update_or_create(
                country_code=country_code,
                snapshot_date=snapshot_date,
                defaults={"pbf_file": pbf_file},
            )
        except Exception as exc:
            logger.warning("Snapshot registration failed (non-fatal): %s", exc)

    def _fail(self, error: str) -> dict:
        logger.error("SnapshotExtractionService: %s", error)
        return {
            "success": False,
            "snapshot_pbf_path": None,
            "poly_file_path": None,
            "entity_count": 0,
            "error": error,
        }
