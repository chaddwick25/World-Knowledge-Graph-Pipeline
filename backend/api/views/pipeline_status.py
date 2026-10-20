"""
WorldKG pipeline state + snapshot job API views.

GET /api/worldkg-pipeline/state/<country_name>/
    Latest ProcessingSession + PipelineRun state for a country.

GET /api/planet/snapshot-dates/
    Settings-derived snapshot-date range + completed SnapshotJob dates.

GET /api/snapshot-jobs/...
    SnapshotJob status + durable per-run results (TaskResult join).
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from core.services.snapshot.regional_path_service import normalize_country_slug
from core.models import ProcessingSession, PipelineRun

logger = logging.getLogger(__name__)


def _country_filter(country_name: str) -> 'Q':
    """Build a Q filter that matches the country name as-sent and as a slug.

    Frontend sends display names ("Ireland And Northern Ireland"); the DB
    stores slugs ("ireland_and_northern_ireland"). Match both so multi-word
    countries resolve.
    """
    from django.db.models import Q
    iso_code = resolve_iso_code(country_name)
    normalized = normalize_country_slug(country_name)
    f = Q(country_name__iexact=country_name) | Q(country_name__iexact=normalized)
    if iso_code and iso_code.upper() != country_name.upper():
        f |= Q(country_name__iexact=iso_code)
    return f


class WorldKGPipelineCountryStateView(APIView):
    """
    GET /api/worldkg-pipeline/state/{country_name}/

    Returns the latest pipeline state for a country, including:
    - ProcessingSession status and configuration
    - PipelineRun stage completion status
    - Whether the pipeline is complete (for summary button enablement)
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name):
        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find the latest ProcessingSession for this country
        session = ProcessingSession.objects.filter(
            session_name__icontains=country_name,
            session_type__in=['WORLDKG_PIPELINE', 'TEMPORAL_CORPUS', 'GEOVECTORS_ENCODING']
        ).order_by('-created_at').first()

        if not session:
            return Response({
                'country_name': country_name,
                'has_pipeline': False,
                'status': 'none',
                'message': 'No pipeline session found for this country'
            })

        # Find associated PipelineRun if exists
        pipeline_run = PipelineRun.objects.filter(
            _country_filter(country_name)
        ).order_by('-created_at').first()

        response_data = {
            'country_name': country_name,
            'has_pipeline': True,
            'session_id': str(session.id),
            'session_type': session.session_type,
            'status': session.status.lower(),
            'created_at': session.created_at.isoformat(),
            'completed_at': session.completed_at.isoformat() if session.completed_at else None,
            'configuration': session.configuration,
            'results': session.results,
        }

        if pipeline_run:
            response_data.update({
                'pipeline_run_id': str(pipeline_run.id),
                'pipeline_status': pipeline_run.status.lower(),
                'current_stage': pipeline_run.current_stage,
                'completed_stages': pipeline_run.completed_stages,
                'stage_metrics': pipeline_run.stage_metrics,
                'pipeline_results': pipeline_run.results,
                'is_complete': pipeline_run.status == 'COMPLETED'
            })
        else:
            # Fallback: check session status for completion
            response_data['is_complete'] = session.status == 'COMPLETED'

        return Response(response_data)


# ══════════════════════════════════════════════════════════════════════════
# Planet / Continent Initialization (Async via Celery Canvas)
# ══════════════════════════════════════════════════════════════════════════


def build_snapshot_dates_payload() -> dict:
    """Settings-derived snapshot-date range + completed SnapshotJob dates.

    Shared by ``SnapshotDatesView`` and ``SystemStatusView`` so the home
    page can hydrate its year selector from a single ``/system/status/``
    ping. Returns::

        {
            "snapshot_dates": ["2025_12_31", "2024_12_31", ...],
            "completed_dates": ["2025_12_31", ...],
            "default": "2025_12_31",
            "count": 5
        }
    """
    from django.conf import settings
    from osmsnapshot.models import SnapshotJob

    start_year = getattr(settings, 'SNAPSHOT_START_YEAR',
                         getattr(settings, 'WORLDKG_SNAPSHOT_START_YEAR', 2021))
    end_year = getattr(settings, 'SNAPSHOT_END_YEAR',
                       getattr(settings, 'WORLDKG_SNAPSHOT_END_YEAR', 2025))
    all_dates = [f"{y}_12_31" for y in range(end_year, start_year - 1, -1)]

    try:
        completed = sorted(
            set(
                SnapshotJob.objects.filter(
                    status=SnapshotJob.Status.COMPLETED,
                ).values_list('snapshot_date', flat=True).distinct()
            ),
            reverse=True,
        )
    except Exception as e:
        logger.error(f"Failed to query completed SnapshotJobs: {e}")
        completed = []

    return {
        'snapshot_dates': all_dates,
        'completed_dates': completed,
        'default': getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31'),
        'count': len(all_dates),
    }


class SnapshotDatesView(APIView):
    """
    GET /api/planet/snapshot-dates/

    Returns the snapshot date range (derived from settings) plus the set of
    dates that have at least one completed ``SnapshotJob``. Replaces the
    legacy filesystem scan of the ``continents/`` directory.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(build_snapshot_dates_payload())


class SnapshotJobStatusView(APIView):
    """
    Snapshot job status — DB ground truth for (country, snapshot_date).

    GET /api/snapshot-jobs/{country_code}/
        → All jobs for this country, with status per snapshot_date.

    GET /api/snapshot-jobs/?snapshot_date=2025_12_31
        → All countries processed for this date.

    GET /api/snapshot-jobs/{country_code}/{snapshot_date}/
        → Single job detail with results summary.
    """
    permission_classes = [AllowAny]

    def get(self, request, country_code=None, snapshot_date=None):
        from osmsnapshot.models import SnapshotJob

        qs = SnapshotJob.objects.all().order_by('-snapshot_date')

        if country_code:
            qs = qs.filter(country_code__iexact=country_code)
        if snapshot_date:
            qs = qs.filter(snapshot_date=snapshot_date)
        # Query-param fallbacks
        qp_date = request.query_params.get('snapshot_date')
        if qp_date and not snapshot_date:
            qs = qs.filter(snapshot_date=qp_date)
        qp_country = request.query_params.get('country_code')
        if qp_country and not country_code:
            qs = qs.filter(country_code__iexact=qp_country)

        jobs = [
            {
                'snapshot_job_id': str(j.id),
                'snapshot_date': j.snapshot_date,
                'country_code': j.country_code,
                'country_name': j.country_name,
                'status': j.status,
                'total_entities': j.total_entities,
                'total_aligned': j.total_aligned,
                'total_spatial_links': j.total_spatial_links,
                'error_message': j.error_message,
                'pipeline_run_id': str(j.pipeline_run_id) if j.pipeline_run_id else None,
                'celery_task_id': j.celery_task_id,
                'started_at': j.started_at.isoformat() if j.started_at else None,
                'completed_at': j.completed_at.isoformat() if j.completed_at else None,
                'created_at': j.created_at.isoformat() if j.created_at else None,
            }
            for j in qs.iterator()
        ]

        # Single-job detail path
        if country_code and snapshot_date and jobs:
            return Response(jobs[0])

        # Country-grouped shape (matches the plan's response example)
        if country_code and not snapshot_date:
            return Response({
                'country_code': country_code,
                'jobs': jobs,
            })

        return Response({'jobs': jobs, 'count': len(jobs)})


class SnapshotJobResultsView(APIView):
    """
    GET /api/snapshot-jobs/{country_code}/{snapshot_date}/results/

    Durable, queryable results layer for a completed snapshot job. Joins
    ``SnapshotJob`` → ``PipelineRun`` → ``TaskResult`` (django-celery-results)
    via ``PipelineAsset.metadata.task_id`` so the frontend can render
    step-by-step results without an active WebSocket connection.

    This is the "durable results" half of Phase E — the WebSocket
    ``_push_update`` channel remains the real-time progress channel during
    execution; this endpoint is the post-completion record.
    """
    permission_classes = [AllowAny]

    def get(self, request, country_code, snapshot_date):
        from osmsnapshot.models import SnapshotJob

        job = SnapshotJob.objects.filter(
            country_code__iexact=country_code, snapshot_date=snapshot_date,
        ).first()
        if not job:
            return Response(
                {'error': f'No SnapshotJob for {country_code}/{snapshot_date}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        run_id = str(job.pipeline_run_id) if job.pipeline_run_id else None
        task_results = []
        if run_id:
            try:
                from api.services.task_result_helpers import (
                    _import_task_result,
                    _collect_task_ids,
                    _serialize_task_results,
                )
                TaskResult = _import_task_result()
                if TaskResult is not None:
                    task_ids = _collect_task_ids(run_id)
                    if task_ids:
                        task_results = _serialize_task_results(TaskResult, task_ids)
                    task_results.sort(
                        key=lambda t: (t.get('date_done') or '', t.get('task_id'))
                    )
            except Exception as exc:
                logger.warning("Failed to load TaskResult rows: %s", exc)

        return Response({
            'snapshot_job_id': str(job.id),
            'country_code': job.country_code,
            'country_name': job.country_name,
            'snapshot_date': job.snapshot_date,
            'status': job.status,
            'total_entities': job.total_entities,
            'total_aligned': job.total_aligned,
            'total_spatial_links': job.total_spatial_links,
            'error_message': job.error_message,
            'pipeline_run_id': run_id,
            'started_at': job.started_at.isoformat() if job.started_at else None,
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
            'task_results': task_results,
        })
