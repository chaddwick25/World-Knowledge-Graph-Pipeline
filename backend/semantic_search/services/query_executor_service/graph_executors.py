"""GraphExecutorsMixin — extracted from query_executor_service.py (monolith split, Phase 5)."""

import logging
from semantic_search.services.entity_geocoder import EntityGeocoder
import time

logger = logging.getLogger(__name__)


class GraphExecutorsMixin:
    """Mixin providing executor methods to QueryExecutorService."""

    @classmethod
    def _execute_spectral_analysis(cls, concepts, country_code,
                                   snapshot_date, trace):
        """SPECTRAL-ANALYSIS — Laplacian spectral features for a region/snapshot.

        Reads the cached GraphSpectralFingerprint from Step 5c.  The
        on-the-fly graph computation path has been removed — Step 5c must
        have run for this template to answer.
        """
        from semantic_search.models import GraphSpectralFingerprint
        from osmsnapshot.models import Snapshot
        snap = cls._get_snapshot_id(snapshot_date)
        snapshot = (
            Snapshot.objects.using('default')
            .filter(country_code__iexact=country_code, snapshot_date=snap)
            .order_by('-created_at').first()
        ) if country_code else None
        if snapshot is not None:
            fp = (
                GraphSpectralFingerprint.objects
                .filter(region=country_code, snapshot=snapshot)
                .order_by('-created_at').first()
            )
            if fp is not None:
                if trace is not None:
                    trace.append({"step": "spectral_features", "source": "db_cache"})
                return {
                    "algebraic_connectivity": fp.algebraic_connectivity,
                    "spectral_gap": fp.spectral_gap,
                    "signal_smoothness": fp.signal_smoothness,
                    "node_count": fp.node_count,
                    "edge_count": fp.edge_count,
                    "eigenvalues": fp.eigenvalues,
                }
        return {"error": "No spectral fingerprint available (Step 5c not run)"}
    @classmethod
    def _execute_temporal_drift(cls, concepts, country_code,
                                snapshot_date, trace):
        """TEMPORAL-DRIFT — spectral drift between snapshots."""
        from semantic_search.models import GraphSpectralDrift

        qs = GraphSpectralDrift.objects.filter(region=country_code)
        if snapshot_date:
            qs = qs.filter(snapshot_to__snapshot_date=snapshot_date)
        drift = qs.order_by('-created_at').first()
        if drift is None:
            return {"error": "No spectral drift available (need >=2 snapshots)"}
        if trace is not None:
            trace.append({
                "step": "drift_metrics",
                "snapshot_from": drift.snapshot_from.snapshot_date,
                "snapshot_to": drift.snapshot_to.snapshot_date,
            })
        return {
            "spectral_distance": drift.spectral_distance,
            "connectivity_delta": drift.connectivity_delta,
            "spectral_gap_delta": drift.spectral_gap_delta,
            "fiedler_drift": drift.fiedler_drift,
            "smoothness_delta": drift.smoothness_delta,
            "drift_magnitude": drift.drift_magnitude,
            "changepoint_detected": drift.changepoint_detected,
            "forecast_eigenvalues": drift.forecast_eigenvalues,
            "snapshot_from": drift.snapshot_from.snapshot_date,
            "snapshot_to": drift.snapshot_to.snapshot_date,
        }
    @classmethod
    def _execute_community_detect(cls, concepts, country_code,
                                  snapshot_date, trace):
        """COMMUNITY-DETECT — Louvain communities + optional class filter.

        Reads stored Louvain assignments from factor_spectral_node_metric
        via FactorResolutionService.community_summary (SQL GROUP BY).
        The on-the-fly NetworkX community detection path has been removed.
        """
        if country_code:
            table_result = cls._community_detect_via_tables(
                concepts, country_code, snapshot_date, trace,
            )
            if table_result is not None:
                return table_result
            if trace is not None:
                trace.append({"step": "factor_join",
                              "warning": "table path unavailable"})
        return {"error": "No community data available (Step 5c not run)"}
    @classmethod
    def _execute_event_diffusion(cls, concepts, country_code,
                                 snapshot_date, trace):
        """EVENT-DIFFUSION — heat kernel diffusion from a source entity.

        SQL-only diffusion via stored eigen-loadings (pgvector <#> query).
        The on-the-fly NetworkX/SciPy heat kernel path has been removed.
        """
        # SUB_COND: source LOCATION → geocode to an OSM entity
        source_concept = cls._get_concept(concepts, "LOCATION")
        if not source_concept or not source_concept.get("text"):
            return {"error": "Event diffusion requires a source location"}

        snap = cls._get_snapshot_id(snapshot_date)
        source_entity = EntityGeocoder.geocode(
            source_concept["text"], country_code, snap
        )
        if not source_entity or not source_entity.get("osm_id"):
            return {"error": f"Could not geocode source: {source_concept['text']}"}
        if trace is not None:
            trace.append({
                "step": "geocode_source",
                "input": source_concept["text"],
                "osm_id": source_entity.get("osm_id"),
            })

        # COND: time window (AMOUNT) → diffusion times
        amount = cls._get_concept(concepts, "AMOUNT")
        t_values = [1.0, 5.0, 10.0]
        if amount and amount.get("text"):
            parsed = cls._parse_radius(amount["text"])
            if parsed:
                # Interpret the radius as a diffusion time scale
                t_values = [float(parsed) / 10.0, float(parsed) / 2.0, float(parsed)]

        # Factor-table path: SQL-only diffusion via stored eigen-loadings.
        if country_code:
            table_result = cls._event_diffusion_via_tables(
                source_entity["osm_id"], t_values, snap, country_code, trace,
            )
            if table_result is not None:
                return table_result
            if trace is not None:
                trace.append({"step": "factor_join",
                              "warning": "table path unavailable"})

        return {"error": "No diffusion data available (Step 5c not run)"}

    # ── Geo + USLP enrichment ───────────────────────────────────────────────
