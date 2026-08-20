"""Celery task: Step 5d — Temporal Drift Analysis.

Computes spectral drift between the current snapshot and the previous
snapshot for the same country, stores a ``GraphSpectralDrift`` row, and
optionally produces an ARIMA / exponential-smoothing forecast of the
next snapshot's eigenvalues plus a CUSUM change-point detection.

**Conditional**: only runs when ≥2 ``GraphSpectralFingerprint`` rows
exist for the country. On the first snapshot, the task body no-ops.

**Non-fatal**: failures are logged and the pipeline continues to Step 6.

References:
- [STATS:Ch3] — Spectral distance, KL divergence
- [STATS:Ch6] — Time series, forecasting, change-point detection
- [DMLS:Ch3] — Batch processing (pre-compute expensive steps)
"""

from __future__ import annotations

import logging

from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_5d_temporal_drift",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("temporal_drift", CountryEnvelope, 5.8)
def step_5d_temporal_drift(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 5d: Compute spectral drift between the current and previous snapshot.

    Conditional: no-ops when <2 snapshots exist for the country.
    Non-fatal: failures are logged and the pipeline continues.
    """
    try:
        _run_temporal_drift(env)
    except Exception as exc:
        _log(
            logger,
            "warning",
            "Step 5d: Temporal drift failed — pipeline continues (non-fatal)",
            country=env.iso,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )

    _log(
        logger,
        "info",
        "Step 5d complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


def _run_temporal_drift(env: CountryEnvelope) -> None:
    """Compute + store the GraphSpectralDrift for ``env``.

    For countries with subgraphs: computes per-subgraph drift (each
    subgraph has its own fingerprint with region=subgraph_slug).
    For countries without subgraphs: computes country-level drift
    (region=country_code) — unchanged.
    """
    import dataclasses
    # Rehydrate subgraphs (same pattern as Step 5c)
    if not env.has_subgraphs or not env.subgraphs:
        try:
            fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
            if fresh.has_subgraphs and fresh.subgraphs:
                env = dataclasses.replace(
                    env,
                    subgraphs=fresh.subgraphs,
                    state=dataclasses.replace(env.state, has_subgraphs=True),
                )
        except Exception:
            pass

    if env.has_subgraphs and env.subgraphs:
        for sg in env.subgraphs:
            _run_drift_for_region(env, sg.slug)
    else:
        _run_drift_for_region(env, env.iso)


def _run_drift_for_region(env: CountryEnvelope, region: str) -> None:
    """Compute + store drift for a single region (subgraph slug or country code)."""
    from osmsnapshot.models import Snapshot
    from semantic_search.models import GraphSpectralFingerprint, GraphSpectralDrift
    from semantic_search.services.spectral_drift_service import SpectralDriftService
    from semantic_search.services.spectral_forecast_service import (
        SpectralForecastService,
    )

    # Resolve the current snapshot
    current_snapshot = (
        Snapshot.objects.using('default')
        .filter(country_code__iexact=env.iso, snapshot_date=env.snapshot_date)
        .order_by('-created_at')
        .first()
    )
    if current_snapshot is None:
        _log(
            logger,
            "info",
            "Step 5d: No Snapshot row — skipping temporal drift",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            pipeline_run_id=env.pipeline_run_id,
        )
        return

    # Load the current fingerprint (just written by Step 5c)
    current_fp = (
        GraphSpectralFingerprint.objects
        .filter(region=region, snapshot=current_snapshot)
        .order_by('-created_at')
        .first()
    )
    if current_fp is None:
        _log(
            logger,
            "info",
            "Step 5d: No current GraphSpectralFingerprint — skipping drift",
            country=env.iso,
            region=region,
            pipeline_run_id=env.pipeline_run_id,
        )
        return

    # Find the previous snapshot (most recent one before the current)
    previous_snapshot = (
        Snapshot.objects.using('default')
        .filter(country_code__iexact=env.iso)
        .exclude(id=current_snapshot.id)
        .filter(snapshot_date__lt=current_snapshot.snapshot_date)
        .order_by('-snapshot_date')
        .first()
    )
    if previous_snapshot is None:
        _log(
            logger,
            "info",
            "Step 5d: No previous snapshot — skipping drift (first run)",
            country=env.iso,
            region=region,
            pipeline_run_id=env.pipeline_run_id,
        )
        return

    previous_fp = (
        GraphSpectralFingerprint.objects
        .filter(region=region, snapshot=previous_snapshot)
        .order_by('-created_at')
        .first()
    )
    if previous_fp is None:
        _log(
            logger,
            "info",
            "Step 5d: Previous snapshot has no fingerprint — skipping drift",
            country=env.iso,
            region=region,
            previous_snapshot=previous_snapshot.snapshot_date,
            pipeline_run_id=env.pipeline_run_id,
        )
        return

    # Compute drift
    drift_svc = SpectralDriftService()
    fp_from = {
        "eigenvalues": previous_fp.eigenvalues,
        "fiedler_vector": previous_fp.fiedler_vector,
        "signal_smoothness": previous_fp.signal_smoothness,
    }
    fp_to = {
        "eigenvalues": current_fp.eigenvalues,
        "fiedler_vector": current_fp.fiedler_vector,
        "signal_smoothness": current_fp.signal_smoothness,
    }
    spectral_drift = drift_svc.compute_spectral_drift(fp_from, fp_to)
    signal_drift = drift_svc.compute_signal_drift(
        previous_fp.signal_smoothness, current_fp.signal_smoothness
    )
    drift_magnitude = drift_svc.classify_drift_magnitude(
        spectral_drift["spectral_distance"]
    )

    # Forecast (needs ≥2 fingerprints; uses all available for this region)
    forecast_eigenvalues = None
    forecast_confidence = None
    changepoint_detected = False
    try:
        all_fps = list(
            GraphSpectralFingerprint.objects
            .filter(region=region)
            .select_related('snapshot')
            .order_by('snapshot__snapshot_date')
        )
        if len(all_fps) >= 2:
            forecast_svc = SpectralForecastService()
            ev_series = [fp.eigenvalues for fp in all_fps]
            fc, ci = forecast_svc.forecast_eigenvalues(ev_series, steps=1)
            if fc.size > 0:
                forecast_eigenvalues = fc.tolist()
                forecast_confidence = ci.tolist()

            # Change-point detection on the spectral distance series
            distances = []
            for i in range(1, len(all_fps)):
                d = drift_svc.compute_spectral_drift(
                    {
                        "eigenvalues": all_fps[i - 1].eigenvalues,
                        "fiedler_vector": all_fps[i - 1].fiedler_vector,
                        "signal_smoothness": all_fps[i - 1].signal_smoothness,
                    },
                    {
                        "eigenvalues": all_fps[i].eigenvalues,
                        "fiedler_vector": all_fps[i].fiedler_vector,
                        "signal_smoothness": all_fps[i].signal_smoothness,
                    },
                )["spectral_distance"]
                distances.append(d)
            if distances:
                cp = forecast_svc.detect_changepoints(distances)
                changepoint_detected = abs(cp["changepoint_magnitude"]) > 0.5
    except Exception as exc:
        _log(
            logger,
            "warning",
            "Step 5d: Forecast / change-point detection failed — continuing",
            country=env.iso,
            region=region,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )

    # Idempotency: replace any existing drift row for this pair
    GraphSpectralDrift.objects.filter(
        region=region,
        snapshot_from=previous_snapshot,
        snapshot_to=current_snapshot,
    ).delete()

    GraphSpectralDrift.objects.create(
        region=region,
        snapshot_from=previous_snapshot,
        snapshot_to=current_snapshot,
        spectral_distance=spectral_drift["spectral_distance"],
        connectivity_delta=spectral_drift["connectivity_delta"],
        spectral_gap_delta=spectral_drift["spectral_gap_delta"],
        fiedler_drift=spectral_drift["fiedler_drift"],
        smoothness_delta=signal_drift["smoothness_delta"],
        drift_magnitude=drift_magnitude,
        forecast_eigenvalues=forecast_eigenvalues,
        forecast_confidence=forecast_confidence,
        changepoint_detected=changepoint_detected,
    )

    _log(
        logger,
        "info",
        "Step 5d: Stored GraphSpectralDrift",
        country=env.iso,
        region=region,
        spectral_distance=spectral_drift["spectral_distance"],
        connectivity_delta=spectral_drift["connectivity_delta"],
        fiedler_drift=spectral_drift["fiedler_drift"],
        drift_magnitude=drift_magnitude,
        changepoint_detected=changepoint_detected,
        pipeline_run_id=env.pipeline_run_id,
    )

    # ── 5d.2: Per-node drift factor rows ──
    # Only run for country-level (subgraph_slug=None) — per-subgraph
    # drift factor rows would need subgraph-scoped DriftNodeMetric rows,
    # which is a future enhancement.  For now, subgraph drift is tracked
    # at the fingerprint level only.
    if region == env.iso:
        try:
            from semantic_search.services.factor_node_writer import FactorNodeWriter

            n_rows = FactorNodeWriter().write_drift_nodes(
                env.iso,
                previous_snapshot.snapshot_date,
                current_snapshot.snapshot_date,
            )
            _log(
                logger,
                "info",
                "Step 5d: Wrote DriftNodeMetric factor rows",
                country=env.iso,
                snapshot_from=previous_snapshot.snapshot_date,
                snapshot_to=current_snapshot.snapshot_date,
                rows=n_rows,
                pipeline_run_id=env.pipeline_run_id,
            )
        except Exception as exc:
            _log(
                logger,
                "warning",
                "Step 5d: Drift-node write failed — pipeline continues (non-fatal)",
                country=env.iso,
                error=str(exc),
                pipeline_run_id=env.pipeline_run_id,
            )
