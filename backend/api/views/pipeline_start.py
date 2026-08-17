"""
WorldKG Unified Pipeline API Views

POST /api/worldkg-pipeline/start/
    Payload: { "country_name": "Jamaica", "pbf_path": "<optional>" }
    Returns: { "session_id": "<uuid>", "ws_url": "ws://..." }

GET /api/worldkg-pipeline/status/<session_id>/
    Returns: ProcessingSession status JSON (fallback for clients without WebSocket)
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from core.models import ProcessingSession, Task, PipelineRun

logger = logging.getLogger(__name__)


class WorldKGPipelineStartView(APIView):
    """DEPRECATED: Use WorldKGPipelineV2StartView (POST /api/worldkg-pipeline-v2/start/) instead."""
    permission_classes = [AllowAny]

    def post(self, request):
        return Response(
            {'error': 'This endpoint is deprecated. Use POST /api/worldkg-pipeline-v2/start/ instead.'},
            status=status.HTTP_410_GONE,
        )


class WorldKGPipelineV2StartView(APIView):
    """
    POST /api/worldkg-pipeline-v2/start/

    Triggers the full WorldKG pipeline for a country (Steps 1-5) via Celery Canvas.

    SnapshotJob gate (TEMPORAL_SNAPSHOT_REFACTOR.md Phase D1):
      - RUNNING exists (without force) → 409 "Currently running"
      - COMPLETED or FAILED exists → delete old record, create new PENDING
        (re-run always allowed; force=True also clears RUNNING)
      - No record → create PENDING
    The created SnapshotJob is linked to the PipelineRun and transitioned to
    RUNNING on dispatch, then to COMPLETED/FAILED by Step 6 / on_failure.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from pipeline.canvas import run_worldkg_pipeline
        from core.models import (
            CountryPipelineProfile, EligibleCountry, PipelineRun,
        )
        from osmsnapshot.models import SnapshotJob
        from django.conf import settings
        from django.utils import timezone

        country_name = request.data.get('country_name', '').strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve ISO code for the country.
        # Order: CountryPipelineProfile → resolve_iso_code → EligibleCountry
        profile = CountryPipelineProfile.objects.filter(
            canonical_name__iexact=country_name
        ).first()
        iso = profile.iso2 if profile else None

        if not iso:
            # Try the resolver (also checks non-sovereign synthetic ISOs)
            iso = resolve_iso_code(country_name)

        if not iso:
            # Fallback: check EligibleCountry (covers split territories like Wales,
            # Scotland, England that have no real ISO code)
            eligible = EligibleCountry.objects.filter(
                country_name__iexact=country_name
            ).first()
            if eligible and eligible.iso_code:
                iso = eligible.iso_code
                logger.info(
                    f"Resolved ISO for {country_name} from EligibleCountry: {iso}"
                )

        if not iso:
            return Response(
                {'error': f'Could not resolve ISO code for {country_name}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        skip_entropy = request.data.get('skip_entropy_gate', False)
        skip_enrich = request.data.get('skip_enrich', False)
        force = request.data.get('force', False)
        snapshot_date = request.data.get('snapshot_date', None) or getattr(
            settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31'
        )

        # ── SnapshotJob gate (atomic — prevents concurrent start races) ──
        from django.db import transaction
        with transaction.atomic():
            existing = SnapshotJob.objects.select_for_update().filter(
                snapshot_date=snapshot_date, country_code__iexact=iso,
            ).first()
            if existing:
                if existing.status == SnapshotJob.Status.RUNNING and not force:
                    return Response(
                        {
                            'error': 'Currently running',
                            'snapshot_date': snapshot_date,
                            'country_code': iso,
                            'snapshot_job_id': str(existing.id),
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                # COMPLETED with force=True → delete + re-run
                # FAILED / stale PENDING → always allow re-run
                existing.delete()

            job = SnapshotJob.objects.create(
                snapshot_date=snapshot_date,
                country_code=iso,
                country_name=country_name,
                status=SnapshotJob.Status.PENDING,
            )

        try:
            run_id = run_worldkg_pipeline(
                iso=iso,
                skip_entropy_gate=skip_entropy,
                skip_enrich=skip_enrich,
                snapshot_date=snapshot_date,
            )
        except ImportError as e:
            job.mark_failed(str(e))
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as e:
            job.mark_failed(str(e))
            raise

        # Link the SnapshotJob to the PipelineRun and transition to RUNNING
        try:
            run = PipelineRun.objects.get(id=run_id)
            job.pipeline_run = run
        except PipelineRun.DoesNotExist:
            run = None
        job.status = SnapshotJob.Status.RUNNING
        job.started_at = timezone.now()
        job.celery_task_id = run_id
        job.save(update_fields=[
            'pipeline_run', 'status', 'started_at', 'celery_task_id',
        ])

        ws_url = f'ws://{settings.WS_HOST}:{settings.WS_PORT}/ws/pipeline/{run_id}/'

        return Response({
            'pipeline_run_id': run_id,
            'ws_url': ws_url,
            'status': 'started',
            'country_name': country_name,
            'snapshot_date': snapshot_date,
            'snapshot_job_id': str(job.id),
        }, status=status.HTTP_202_ACCEPTED)


class PlanetInitializeView(APIView):
    """POST /api/planet/initialize/

    Planet initialization is now a Docker entrypoint step
    (``python manage.py init_planet``) rather than a Celery canvas — see
    ``docs/plans/CORE_APP_CONSOLIDATION_PLAN.md``. This endpoint is kept
    as a status-only facade: it refuses to dispatch a new run and instead
    reports the latest ``PlanetSnapshot`` status so the frontend can
    surface it. To re-run init manually, exec into the backend container
    and run ``python manage.py init_planet``.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from core.models import PlanetSnapshot

        latest = (
            PlanetSnapshot.objects.order_by("-snapshot_date").first()
        )
        return Response(
            {
                'status': 'noop',
                'message': (
                    'Planet init is now a Docker startup step '
                    '(python manage.py init_planet). POST is a no-op. '
                    'GET /api/planet/status/ for the latest snapshot status.'
                ),
                'latest_snapshot_date': (
                    latest.snapshot_date_str if latest else None
                ),
                'latest_snapshot_status': latest.status if latest else None,
            },
            status=status.HTTP_410_GONE,
        )


class PlanetInitStatusView(APIView):
    """GET /api/planet/status/<pipeline_run_id>/

    Planet init no longer creates a ``PipelineRun`` row — status is now
    tracked on ``PlanetSnapshot``. The path param is kept for URL
    backwards-compat but ignored; we return the latest snapshot's status.
    """
    permission_classes = [AllowAny]

    def get(self, request, pipeline_run_id):
        from core.models import PlanetSnapshot

        latest = PlanetSnapshot.objects.order_by("-snapshot_date").first()
        if not latest:
            return Response(
                {'error': 'No PlanetSnapshot rows found — init_planet has not run yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({
            'pipeline_run_id': str(latest.id),
            'snapshot_date': latest.snapshot_date_str,
            'status': latest.status,
            'started_at': latest.created_at.isoformat() if latest.created_at else None,
            'completed_at': latest.completed_at.isoformat() if latest.completed_at else None,
        })


