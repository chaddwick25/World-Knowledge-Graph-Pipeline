"""Shared country-pipeline dispatch — the SnapshotJob gate + entry point.

Extracted from ``api/views/pipeline_start.py`` (ISSUE-admin-job-control-plane)
so the Django admin operator surface and the legacy key-gated API view share
one race-safe implementation of:

  1. Country name → ISO resolution (CountryPipelineProfile → resolver →
     EligibleCountry)
  2. Snapshot-date floor validation (``SNAPSHOT_DATE_FLOOR``)
  3. SnapshotJob gate (atomic): RUNNING blocks (unless force), otherwise
     delete-and-recreate PENDING
  4. Dispatch via ``pipeline.canvas.run_worldkg_pipeline``
  5. Link SnapshotJob → PipelineRun, transition to RUNNING

Pure data plane — no DRF, no admin. Callers map ``PipelineDispatchError``
onto their own surface (409 Response, admin form validation error).
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

_SNAPSHOT_DATE_RE = re.compile(r"^\d{4}_\d{2}_\d{2}$")


class PipelineDispatchError(Exception):
    """Dispatch rejected — ``code`` maps to the caller's error surface.

    Codes: ``invalid`` (bad input / floor), ``conflict`` (already running),
    ``unavailable`` (dispatch infrastructure failure).
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def resolve_country_iso(country_name: str):
    """Resolve a country name to an ISO 3166-1 alpha-2 code.

    Order: CountryPipelineProfile → osm_wikidata resolver (covers
    non-sovereign synthetic ISOs) → EligibleCountry (split territories
    like Wales / Scotland that have no real ISO code).
    """
    from core.models import CountryPipelineProfile, EligibleCountry
    from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code

    profile = CountryPipelineProfile.objects.filter(
        canonical_name__iexact=country_name
    ).first()
    iso = profile.iso2 if profile else None
    if not iso:
        iso = resolve_iso_code(country_name)
    if not iso:
        eligible = EligibleCountry.objects.filter(
            country_name__iexact=country_name
        ).first()
        if eligible and eligible.iso_code:
            iso = eligible.iso_code
            logger.info(
                "Resolved ISO for %s from EligibleCountry: %s",
                country_name, iso,
            )
    return iso


def validate_snapshot_date_floor(snapshot_date: str) -> None:
    """Reject snapshot dates before ``SNAPSHOT_DATE_FLOOR``.

    The floor is the pretrained encoder's knowledge boundary — snapshots
    older than the model's training era would produce anachronistic
    embeddings. Raises ``PipelineDispatchError(code='invalid')``.
    """
    floor = getattr(settings, "SNAPSHOT_DATE_FLOOR", "2020_09")
    floor_date = f"{floor}_01"
    if not _SNAPSHOT_DATE_RE.match(snapshot_date or ""):
        raise PipelineDispatchError(
            f"snapshot_date must be YYYY_MM_DD (e.g. 2025_12_31), "
            f"got {snapshot_date!r}"
        )
    if snapshot_date < floor_date:
        raise PipelineDispatchError(
            f"snapshot_date {snapshot_date} predates the encoder's "
            f"knowledge boundary (floor {floor}); earlier snapshots "
            f"would be anachronistic"
        )


def dispatch_country_pipeline(
    country_name: str,
    snapshot_date: str = None,
    skip_entropy_gate: bool = False,
    skip_enrich: bool = False,
    force: bool = False,
) -> dict:
    """Gate + dispatch a full country pipeline run (Steps 1-6).

    Returns a dict with ``pipeline_run_id``, ``snapshot_job_id``,
    ``iso``, ``country_name``, ``snapshot_date``. Raises
    ``PipelineDispatchError`` on invalid input, floor violation, or a
    currently-running job for the same (country, snapshot_date).
    """
    from django.db import transaction
    from django.utils import timezone

    from osmsnapshot.models import SnapshotJob

    country_name = (country_name or "").strip()
    if not country_name:
        raise PipelineDispatchError("country_name is required")

    iso = resolve_country_iso(country_name)
    if not iso:
        raise PipelineDispatchError(
            f"Could not resolve ISO code for {country_name}"
        )

    snapshot_date = snapshot_date or getattr(
        settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31"
    )
    validate_snapshot_date_floor(snapshot_date)

    # ── SnapshotJob gate (atomic — prevents concurrent start races) ──
    with transaction.atomic():
        existing = SnapshotJob.objects.select_for_update().filter(
            snapshot_date=snapshot_date, country_code__iexact=iso,
        ).first()
        if existing:
            if existing.status == SnapshotJob.Status.RUNNING and not force:
                raise PipelineDispatchError(
                    f"Currently running: {country_name} @ {snapshot_date} "
                    f"(snapshot_job_id={existing.id})",
                    code="conflict",
                )
            # COMPLETED → delete + re-run (re-run always allowed);
            # FAILED / stale PENDING → always allow re-run.
            existing.delete()

        job = SnapshotJob.objects.create(
            snapshot_date=snapshot_date,
            country_code=iso,
            country_name=country_name,
            status=SnapshotJob.Status.PENDING,
        )

    try:
        from pipeline.canvas import run_worldkg_pipeline
        run_id = run_worldkg_pipeline(
            iso=iso,
            skip_entropy_gate=skip_entropy_gate,
            skip_enrich=skip_enrich,
            snapshot_date=snapshot_date,
        )
    except ImportError as exc:
        job.mark_failed(str(exc))
        raise PipelineDispatchError(str(exc), code="unavailable") from exc
    except Exception as exc:
        job.mark_failed(str(exc))
        raise

    # Link the SnapshotJob to the PipelineRun and transition to RUNNING.
    from core.models import PipelineRun
    try:
        run = PipelineRun.objects.get(id=run_id)
        job.pipeline_run = run
    except PipelineRun.DoesNotExist:
        pass
    job.status = SnapshotJob.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = run_id
    job.save(update_fields=[
        "pipeline_run", "status", "started_at", "celery_task_id",
    ])

    return {
        "pipeline_run_id": run_id,
        "snapshot_job_id": str(job.id),
        "iso": iso,
        "country_name": country_name,
        "snapshot_date": snapshot_date,
    }
