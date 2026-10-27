"""Snapshot backfill — sequential monthly chain (ISSUE-admin-job-control-plane).

One Celery task per month, chained sequentially (one-GPU discipline):

    osmium time-filter (preprocess_snapshot) → upsert-only OsmEntity write
    (no FastText encode) → Step 5c spectral solve → Step 5d drift against
    the previous month.

Failure policy: **fail-hard** — a failed month halts the chain (Celery
chains stop on first failure; no non-fatal month wrapper). The failed
month's SnapshotJob row is marked FAILED with the error by the task body;
resubmission resumes from it (COMPLETED months are skipped at submit).

Backfill snapshots are drift-source, not queryable: their SnapshotJob rows
carry ``is_drift_source=True`` so the frontend calendar never offers them
as searchable dates.
"""

from __future__ import annotations

import calendar
import logging
import re

from django.conf import settings

logger = logging.getLogger("pipeline")

_MONTH_RE = re.compile(r"^(\d{4})_(\d{2})$")


class BackfillError(Exception):
    """Backfill submit rejected — message is operator-facing."""


# ── Month helpers ────────────────────────────────────────────────────────
def parse_month(value: str):
    """Parse ``YYYY_MM`` → (year, month). Raises BackfillError on bad input."""
    m = _MONTH_RE.match((value or "").strip())
    if not m:
        raise BackfillError(f"month must be YYYY_MM (e.g. 2024_03), got {value!r}")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise BackfillError(f"month out of range: {value!r}")
    return year, month


def month_end_snapshot_date(value: str) -> str:
    """``YYYY_MM`` → month-end snapshot date ``YYYY_MM_DD``.

    Month-end pinning matches the yearly convention (``2025_12_31``) and
    gives osmium time-filter the full month of history.
    """
    year, month = parse_month(value)
    return f"{year}_{month:02d}_{calendar.monthrange(year, month)[1]:02d}"


def iter_months(start_month: str, end_month: str):
    """Yield ``YYYY_MM`` strings from start to end, inclusive."""
    y, m = parse_month(start_month)
    ey, em = parse_month(end_month)
    while (y, m) <= (ey, em):
        yield f"{y}_{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ── Submit-side validation + chain dispatch ──────────────────────────────
def run_snapshot_backfill(
    country_name: str,
    start_month: str,
    end_month: str,
) -> dict:
    """Validate + enqueue a sequential monthly backfill chain.

    Reuses the shared ISO resolver. Validation:
      - start floor ``SNAPSHOT_DATE_FLOOR`` (OSM history depth + drift-series
        coherence; backfill months skip the encoder entirely)
      - end ceiling = the country's latest snapshot month
      - inter-run GPU exclusion: rejected while ANY pipeline is RUNNING
        (the per-iso guard in ``run_worldkg_pipeline`` does not cover
        cross-run GPU contention)

    SnapshotJob gate per month: COMPLETED → skipped (nothing to backfill);
    RUNNING → whole submit rejected; FAILED / stale PENDING → deleted and
    recreated as drift-source PENDING.

    Returns a dict with ``pipeline_run_id``, ``months`` (dispatched),
    ``skipped_months``. Raises ``BackfillError`` on validation failure.
    """
    from django.utils import timezone

    from core.models import PipelineRun
    from core.services.pipeline.run_dispatch_service import (
        PipelineDispatchError,
        resolve_country_iso,
    )
    from osmsnapshot.models import Snapshot, SnapshotJob

    country_name = (country_name or "").strip()
    if not country_name:
        raise BackfillError("country_name is required")

    iso = resolve_country_iso(country_name)
    if not iso:
        raise BackfillError(f"Could not resolve ISO code for {country_name}")

    # ── Floor (encoder era / OSM history depth) ──
    floor = getattr(settings, "SNAPSHOT_DATE_FLOOR", "2020_09")
    if parse_month(start_month) < parse_month(floor):
        raise BackfillError(
            f"start month {start_month} predates the floor {floor} "
            f"(the encoder's knowledge boundary; earlier snapshots would "
            f"be anachronistic and OSM history is too thin for a coherent "
            f"drift series)"
        )
    if parse_month(start_month) > parse_month(end_month):
        raise BackfillError(
            f"start month {start_month} is after end month {end_month}"
        )

    # ── End ceiling = the country's latest snapshot ──
    latest = (
        Snapshot.objects.filter(country_code__iexact=iso)
        .order_by("-snapshot_date")
        .values_list("snapshot_date", flat=True)
        .first()
    )
    if not latest:
        raise BackfillError(
            f"No snapshots exist for {country_name} — run the country "
            f"pipeline first (backfill drifts against existing snapshots)"
        )
    latest_year, latest_month = int(latest[:4]), int(latest[5:7])
    if parse_month(end_month) > (latest_year, latest_month):
        raise BackfillError(
            f"end month {end_month} is after {country_name}'s latest "
            f"snapshot ({latest})"
        )

    # ── Inter-run GPU exclusion ──
    from datetime import timedelta
    from django.utils import timezone

    zombie_cutoff = timezone.now() - timedelta(hours=24)
    running = PipelineRun.objects.filter(
        status=PipelineRun.PipelineStatus.RUNNING,
        updated_at__gte=zombie_cutoff,
    ).exists()
    if running:
        raise BackfillError(
            "A pipeline is currently RUNNING — the backfill's spectral "
            "solves would contend for the single GPU. Submit again once "
            "it finishes."
        )

    # ── Per-month SnapshotJob gate ──
    months, skipped = [], []
    for month in iter_months(start_month, end_month):
        snapshot_date = month_end_snapshot_date(month)
        existing = SnapshotJob.objects.filter(
            snapshot_date=snapshot_date, country_code__iexact=iso,
        ).first()
        if existing:
            # SnapshotJob has no updated_at — started_at (set when the
            # month task begins) is the freshness proxy; fall back to
            # created_at for a job that never started.
            job_fresh_at = existing.started_at or existing.created_at
            if (existing.status == SnapshotJob.Status.RUNNING
                    and job_fresh_at and job_fresh_at >= zombie_cutoff):
                raise BackfillError(
                    f"Month {month} is currently RUNNING for {country_name} "
                    f"(snapshot_job_id={existing.id})"
                )
            if existing.status == SnapshotJob.Status.COMPLETED:
                skipped.append(month)
                continue
            # FAILED / stale PENDING / stale RUNNING → delete + recreate
            existing.delete()
        job = SnapshotJob.objects.create(
            snapshot_date=snapshot_date,
            country_code=iso,
            country_name=country_name,
            status=SnapshotJob.Status.PENDING,
            is_drift_source=True,
        )
        months.append((month, snapshot_date, str(job.id)))

    if not months:
        return {
            "dispatched": False,
            "pipeline_run_id": None,
            "months": [],
            "skipped_months": skipped,
            "message": "All months in range already completed — nothing to backfill",
        }

    # ── PipelineRun (chain tracking) ──
    run = PipelineRun.objects.create(
        country_code=iso,
        country_name=country_name,
        pipeline_type="snapshot_backfill",
        status=PipelineRun.PipelineStatus.PENDING,
        configuration={
            "start_month": start_month,
            "end_month": end_month,
            "months": [sd for _, sd, _ in months],
        },
    )
    for _, _, job_id in months:
        SnapshotJob.objects.filter(id=job_id).update(pipeline_run=run)

    # ── Sequential chain: one task per month ──
    from celery import chain

    chain(
        *[
            backfill_month_task.s(
                iso=iso,
                country_name=country_name,
                snapshot_date=snapshot_date,
                backfill_run_id=str(run.id),
                snapshot_job_id=job_id,
            )
            for _, snapshot_date, job_id in months
        ]
    ).apply_async(task_id=str(run.id))

    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = timezone.now()
    run.started_at = timezone.now()
    run.save(update_fields=["status", "queued_at", "started_at"])

    return {
        "dispatched": True,
        "pipeline_run_id": str(run.id),
        "months": [sd for _, sd, _ in months],
        "skipped_months": skipped,
        "message": None,
    }


# ── The monthly task ─────────────────────────────────────────────────────
from pipeline.celery_app import PipelineTask, pipeline_task  # noqa: E402


@pipeline_task(
    bind=True, base=PipelineTask,
    name="backfill_month",
    max_retries=1, default_retry_delay=60,
)
def backfill_month_task(
    self,
    prev_result=None,
    iso=None,
    country_name=None,
    snapshot_date=None,
    backfill_run_id=None,
    snapshot_job_id=None,
) -> dict:
    """Celery seam for one backfill month.

    ``prev_result`` is the previous month's result dict (Celery chain
    semantics — ignored; each month resolves its own state from the DB).
    """
    return _backfill_month(
        task_id=getattr(self.request, "id", None),
        iso=iso,
        country_name=country_name,
        snapshot_date=snapshot_date,
        backfill_run_id=backfill_run_id,
        snapshot_job_id=snapshot_job_id,
    )


def _finalize_backfill_run(backfill_run_id: str) -> None:
    """Mark the backfill PipelineRun COMPLETED when no jobs remain open.

    Called after each successful month; the last month in the chain
    finalizes the run. Never raises.
    """
    try:
        from core.models import PipelineRun
        from osmsnapshot.models import SnapshotJob

        open_jobs = SnapshotJob.objects.filter(
            pipeline_run_id=backfill_run_id,
        ).exclude(status=SnapshotJob.Status.COMPLETED).exists()
        if open_jobs:
            return
        run = PipelineRun.objects.filter(id=backfill_run_id).first()
        if run is not None and run.status == PipelineRun.PipelineStatus.RUNNING:
            run.mark_completed(results={"type": "snapshot_backfill"})
    except Exception as exc:
        logger.warning("Backfill run finalization failed: %s", exc)


def _backfill_month(
    task_id: str,
    iso: str,
    country_name: str,
    snapshot_date: str,
    backfill_run_id: str,
    snapshot_job_id: str,
) -> dict:
    """One month: extract → upsert-only → 5c spectral → 5d drift.

    Fail-hard: any exception marks the SnapshotJob FAILED and re-raises
    (the chain halts; resubmission resumes from this month).
    """
    from pathlib import Path

    from osmsnapshot.models import SnapshotJob

    job = SnapshotJob.objects.filter(id=snapshot_job_id).first()
    if job is None:
        raise RuntimeError(
            f"SnapshotJob {snapshot_job_id} not found for "
            f"{iso} @ {snapshot_date}"
        )

    try:
        # pipeline_run FK was set at submit time; record the Celery task id.
        job.mark_running(celery_task_id=task_id)

        from pipeline.country_envelope import CountryEnvelope
        from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral import (
            _run_spectral_analysis,
        )
        from pipeline.tasks.country_pipeline_steps.step_5d_temporal_drift import (
            _run_temporal_drift,
        )
        from pipeline.tasks.helper import preprocess_snapshot

        env = CountryEnvelope.from_db(iso, snapshot_date=snapshot_date)
        env = env.with_state(pipeline_run_id=backfill_run_id)

        # 1. osmium extract + time-filter → snapshot PBF + Snapshot row
        #    (short-circuits when the PBF already exists)
        env = preprocess_snapshot(env, logger)

        # 1b. Ensure the leaf partition exists for this (snapshot, country)
        #     — the vectors root is partitioned BY LIST (snapshot_id), so
        #     a brand-new snapshot date has no leaf and the upsert would
        #     fail with "no partition of relation found" (the 2026-10-26
        #     JM shakedown failure). Same call Step 1 makes; drift-source
        #     leaves skip data migration / MV / HNSW (no embeddings).
        from django.core.management import call_command
        call_command(
            "create_country_partitions",
            country=iso,
            snapshot=snapshot_date,
            skip_data=True,
            skip_mv=True,
            skip_hnsw=True,
        )

        # 2. Upsert-only OsmEntity write (no FastText encode; enrichment
        #    runs SQL-side for the 5c class signal)
        from core.services.snapshot.embedding_service import EmbeddingService
        result = EmbeddingService(Path(settings.EMBEDDINGS_ROOT)).run(
            env, upsert_only=True,
        )

        # 3. Step 5c spectral solve (writes GraphSpectralFingerprint +
        #    factor_spectral_node_metric rows)
        _run_spectral_analysis(env)

        # 4. Step 5d drift against the previous month (no-ops on the
        #    first month / missing fingerprints)
        _run_temporal_drift(env)

        job.mark_completed(total_entities=result.get("entity_count", 0))
        _finalize_backfill_run(backfill_run_id)
    except Exception as exc:
        logger.error(
            "Backfill month failed [country=%s date=%s]: %s",
            iso, snapshot_date, exc, exc_info=True,
        )
        try:
            job.mark_failed(str(exc))
        except Exception:
            pass
        try:
            from core.models import PipelineRun
            run = PipelineRun.objects.filter(id=backfill_run_id).first()
            if run is not None:
                run.mark_failed(f"Backfill month {snapshot_date} failed: {exc}")
        except Exception:
            pass
        raise

    logger.info(
        "Backfill month complete [country=%s date=%s entities=%s]",
        iso, snapshot_date, result.get("entity_count", 0),
    )
    return {
        "iso": iso,
        "country_name": country_name,
        "snapshot_date": snapshot_date,
        "entity_count": result.get("entity_count", 0),
        "backfill_run_id": backfill_run_id,
    }
