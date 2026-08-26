"""
Regression tests for the country slug gate (fixes 1 + 2, 2026-08-26).

Reproduces the NL pipeline crash: ``SnapshotExtractionService`` derived its
output directory from ``canonical_name`` ("Kingdom of the Netherlands" →
"kingdom_of_the_netherlands") while ``CountryEnvelope.from_db`` resolved
``snapshot_pbf_path`` from ``canonical_slug`` ("netherlands"). Step 1 then
failed with ``RuntimeError: Open failed for '.../netherlands/...'``.

Two fixes are covered here:
  1. ``extract_country_snapshot`` accepts a ``country_slug`` parameter — the
     ``canonical_slug`` gate — so extraction writes where the envelope expects.
  2. ``preprocess_snapshot`` returns a REPLACED envelope (the input is a frozen
     dataclass) instead of silently failing to mutate ``snapshot_pbf_path``.

No DB / network / GPU required.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.services.snapshot.country_override_service import get_country_slug
from core.services.snapshot.regional_path_service import (
    RegionalPathService,
    normalize_country_slug,
)
from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger("test_snapshot_slug_gate")


# =============================================================================
# 1. Fix 1 — SnapshotExtractionService respects the canonical_slug gate
# =============================================================================


class TestExtractCountrySnapshotSlugGate:
    """extract_country_snapshot(country_slug=...) must write where the
    envelope expects — NOT where canonical_name slugifies to."""

    def _run_extraction(self, tmp_path, country_slug):
        """Run a full (mocked-osmium) extraction into a tmp base dir."""
        from core.services.snapshot.snapshot_extraction_service import (
            SnapshotExtractionService,
        )

        base = RegionalPathService(str(tmp_path))
        with patch(
            "core.services.snapshot.snapshot_extraction_service.regional_path_service",
            base,
        ), patch.object(
            SnapshotExtractionService, "_resolve_continent_pbf",
            return_value=Path("/fake/continents/europe.pbf"),
        ), patch.object(
            SnapshotExtractionService, "_resolve_poly_file",
            return_value=Path("/fake/poly/europe/netherlands.poly"),
        ), patch.object(
            SnapshotExtractionService, "_osmium_extract_polygon",
            return_value=True,
        ), patch.object(
            SnapshotExtractionService, "_osmium_time_filter",
            return_value=True,
        ), patch.object(
            SnapshotExtractionService, "_register_pbf_file",
            return_value=MagicMock(),
        ), patch.object(
            SnapshotExtractionService, "_register_snapshot",
        ):
            # The extraction validates the output file exists post-time-filter.
            snap_path = base.get_single_snapshot_pbf_path(
                "europe", country_slug, "2025_12_31",
            )
            snap_path.parent.mkdir(parents=True, exist_ok=True)
            snap_path.write_bytes(b"\x00" * 10)

            service = SnapshotExtractionService()
            service.osmium = MagicMock()
            service.osmium.file_info.return_value = {
                "exit_code": 0,
                "info": {"objects": {"nodes": 5, "ways": 3, "relations": 1}},
            }

            return service.extract_country_snapshot(
                country_code="NL",
                country_name="Kingdom of the Netherlands",
                continent="europe",
                snapshot_date="2025_12_31",
                osm_relation_id=2323309,
                country_slug=country_slug,
            )

    @pytest.mark.unit
    def test_gate_slug_controls_output_directory(self, tmp_path):
        """With country_slug='netherlands' the PBF lands under netherlands/,
        matching CountryEnvelope.from_db's snapshot_pbf_path (the crash)."""
        result = self._run_extraction(tmp_path, country_slug="netherlands")

        assert result["success"] is True, result.get("error")
        path = result["snapshot_pbf_path"]
        assert "netherlands" in path
        assert "kingdom_of_the_netherlands" not in path, (
            "extraction must NOT use the canonical_name-derived slug"
        )
        assert path.endswith("netherlands/temporal_snapshots/netherlands_2025_12_31.osm.pbf")

    @pytest.mark.unit
    def test_gate_slug_matches_envelope_path(self, tmp_path):
        """The extraction path equals CountryEnvelope.from_db's expected path
        when the canonical_slug gate is passed."""
        result = self._run_extraction(tmp_path, country_slug="netherlands")

        # What from_db computes as the fallback snapshot_pbf_path
        # (candidate_slugs[0] = get_country_slug("NL", canonical_slug)).
        env_expected = RegionalPathService(str(tmp_path)).get_single_snapshot_pbf_path(
            "europe", get_country_slug("NL", "netherlands"), "2025_12_31",
        )
        assert result["snapshot_pbf_path"] == str(env_expected)

    @pytest.mark.unit
    def test_legacy_derivation_would_have_mismatched(self, tmp_path):
        """Demonstrate the original bug mechanism: deriving from canonical_name
        produces a different directory than the canonical_slug gate."""
        base = RegionalPathService(str(tmp_path))
        gate_path = base.get_single_snapshot_pbf_path(
            "europe", "netherlands", "2025_12_31",
        )
        legacy_path = base.get_single_snapshot_pbf_path(
            "europe", normalize_country_slug("Kingdom of the Netherlands"),
            "2025_12_31",
        )
        assert str(gate_path) != str(legacy_path)
        assert "kingdom_of_the_netherlands" in str(legacy_path)

    @pytest.mark.unit
    def test_override_still_applies_on_top_of_gate_slug(self, tmp_path):
        """get_country_slug(iso, canonical_slug) applies overrides.json on top
        of the gate (e.g. IE: ireland-and-northern-ireland → ireland)."""
        overrides = {
            "country_iso_overrides": {
                "IE": {"country_slug": "ireland"},
            }
        }
        with patch(
            "core.services.snapshot.country_override_service.load_overrides",
            return_value=overrides,
        ):
            assert get_country_slug("IE", "ireland-and-northern-ireland") == "ireland"
            assert get_country_slug("NL", "netherlands") == "netherlands"


# =============================================================================
# 2. Fix 2 — preprocess_snapshot returns a replaced envelope (frozen-safe)
# =============================================================================


class TestPreprocessSnapshotReturnsEnvelope:
    """preprocess_snapshot must return a new envelope with the extraction paths
    instead of mutating the frozen input (which silently failed before)."""

    def _build_env(self):
        return CountryEnvelope.from_dict({
            "iso": "NL",
            "name": "Kingdom of the Netherlands",
            "slug": "netherlands",
            "continent": "europe",
            "osm_relation_id": 2323309,
            "snapshot_date": "2025_12_31",
            "snapshot_pbf_path": (
                "/data/OSM-PBF-FILES/osm_wikidata_extractions/europe/netherlands/"
                "temporal_snapshots/netherlands_2025_12_31.osm.pbf"
            ),
        })

    @pytest.mark.unit
    def test_returns_replaced_envelope_with_extraction_paths(self):
        """The returned envelope carries the actual extraction paths; the
        original frozen envelope is untouched."""
        from pipeline.tasks import helper

        env = self._build_env()
        original_path = env.snapshot_pbf_path
        assert Path(env.snapshot_pbf_path).exists() is False  # pre-conditions

        mock_result = {
            "success": True,
            "snapshot_pbf_path": (
                "/data/OSM-PBF-FILES/osm_wikidata_extractions/europe/netherlands/"
                "temporal_snapshots/netherlands_2025_12_31.osm.pbf"
            ),
            "poly_file_path": "/data/osm_polygon_files/europe/netherlands.poly",
            "entity_count": 9,
            "skipped": False,
            "error": None,
        }

        with patch(
            "core.services.snapshot.snapshot_extraction_service.SnapshotExtractionService"
        ) as mock_cls:
            mock_cls.return_value.extract_country_snapshot.return_value = mock_result

            new_env = helper.preprocess_snapshot(env, logger=logger)

        # The extraction service received the canonical_slug gate.
        mock_cls.return_value.extract_country_snapshot.assert_called_once()
        call_kwargs = mock_cls.return_value.extract_country_snapshot.call_args.kwargs
        assert call_kwargs.get("country_slug") == "netherlands"

        # A NEW envelope is returned with the actual extraction paths.
        assert new_env is not env
        assert new_env.snapshot_pbf_path == mock_result["snapshot_pbf_path"]
        assert new_env.poly_path == mock_result["poly_file_path"]

        # The original envelope is unchanged (frozen dataclass — no mutation).
        assert env.snapshot_pbf_path == original_path
        assert env.poly_path is None

    @pytest.mark.unit
    def test_already_exists_branch_returns_same_envelope(self):
        """When the snapshot already exists on disk, the envelope is returned
        as-is (no extraction, no DB writes)."""
        from pipeline.tasks import helper

        with patch("pipeline.tasks.helper.Path.exists", return_value=True), patch(
            "pipeline.tasks.helper._ensure_snapshot_row"
        ) as mock_ensure:
            env = self._build_env()
            out = helper.preprocess_snapshot(env, logger=logger)

        assert out is env
        mock_ensure.assert_called_once_with(env, logger)

    @pytest.mark.unit
    def test_no_osm_relation_id_returns_envelope(self):
        """Missing OSM relation ID → warning path returns the envelope as-is."""
        from pipeline.tasks import helper

        env = CountryEnvelope.from_dict({
            "iso": "NL",
            "name": "Kingdom of the Netherlands",
            "slug": "netherlands",
            "continent": "europe",
            "osm_relation_id": None,
            "snapshot_date": "2025_12_31",
        })
        out = helper.preprocess_snapshot(env, logger=logger)
        assert out is env

    @pytest.mark.unit
    def test_extraction_failure_raises(self):
        """A failed extraction still raises (unchanged contract)."""
        from pipeline.tasks import helper

        env = self._build_env()
        with patch(
            "core.services.snapshot.snapshot_extraction_service.SnapshotExtractionService"
        ) as mock_cls:
            mock_cls.return_value.extract_country_snapshot.return_value = {
                "success": False,
                "error": "Continent PBF not found for 'europe'.",
            }
            with pytest.raises(RuntimeError, match="Snapshot extraction failed for NL"):
                helper.preprocess_snapshot(env, logger=logger)
