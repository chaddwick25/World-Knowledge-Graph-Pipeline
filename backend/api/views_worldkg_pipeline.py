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

# NOTE: WorldKGPipelineService imported lazily — it triggers heavy Django app resolution.
from orchestration.models import ProcessingSession, Task, PipelineRun

logger = logging.getLogger(__name__)


class WorldKGPipelineStartView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        country_name = request.data.get('country_name', '').strip()
        pbf_path = request.data.get('pbf_path', '')
        skip_preflight = request.data.get('skip_preflight', False)

        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        session = ProcessingSession.objects.create(
            session_name=f'WorldKG Pipeline: {country_name}',
            session_type='WORLDKG_PIPELINE',
            status='IN_PROGRESS',
            configuration={
                'country_name': country_name,
                'pbf_path': pbf_path,
                'skip_preflight': skip_preflight,
            }
        )

        Task.objects.create(
            task_type=Task.TaskType.WORLDKG_PIPELINE,
            processing_session=session,
            status=Task.TaskStatus.IN_PROGRESS,
            parameters={'country_name': country_name, 'pbf_path': pbf_path, 'skip_preflight': skip_preflight},
        )

        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        svc = WorldKGPipelineService(
            session_id=str(session.id),
            country_name=country_name,
            pbf_path=pbf_path or None,
            skip_preflight=skip_preflight,
        )
        svc.run_in_background()

        ws_url = f'ws://localhost:8000/ws/pipeline/{session.id}/'
        return Response({
            'session_id': str(session.id),
            'ws_url': ws_url,
            'country_name': country_name,
            'status': 'started',
        }, status=status.HTTP_202_ACCEPTED)


class WorldKGPipelineStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        try:
            session = ProcessingSession.objects.get(id=session_id)
        except ProcessingSession.DoesNotExist:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        task = Task.objects.filter(
            processing_session=session,
            task_type=Task.TaskType.WORLDKG_PIPELINE
        ).first()

        return Response({
            'session_id': str(session.id),
            'status': session.status,
            'configuration': session.configuration,
            'results': session.results,
            'created_at': session.created_at,
            'completed_at': session.completed_at,
            'task_status': task.status if task else None,
        })


class WorldKGPipelineSummaryView(APIView):
    """
    GET /api/worldkg-pipeline/summary/{country_name}/

    Returns summarised SpatialTripletScore data for a country:
    - Total links, accepted count, acceptance rate
    - Relation distribution with counts and acceptance rates
    - Score distribution histogram (0.2 buckets)
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name):
        from collections import Counter, defaultdict
        from igea.models import SpatialTripletScore
        from orchestration.models import PipelineRun

        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Primary filter: country_name (self-describing data)
        # Optional filter: snapshot_id (for specific snapshot queries)
        snapshot_id = request.query_params.get('snapshot_id')

        # Build query - always filter by country_name (also try ISO code)
        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        from django.db.models import Q
        iso_code = WorldKGPipelineService._resolve_iso_code(country_name)
        country_filter = Q(country_name__iexact=country_name)
        if iso_code and iso_code.upper() != country_name.upper():
            country_filter |= Q(country_name__iexact=iso_code)
        scores = SpatialTripletScore.objects.filter(country_filter)

        # If snapshot_id provided, also filter by it
        if snapshot_id:
            scores = scores.filter(snapshot_id=snapshot_id)
            logger.info(f"Filtering by country_name={country_name} and snapshot_id={snapshot_id}")
        else:
            logger.info(f"Filtering by country_name={country_name} (no snapshot_id filter)")

        logger.info(f"Query will return {scores.count()} accepted triplet scores")

        total_links = 0
        accepted_links = 0
        relation_counts = Counter()
        relation_accepted = Counter()
        score_buckets = Counter()

        bucket_edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        bucket_labels = ['0.0-0.2', '0.2-0.4', '0.4-0.6', '0.6-0.8', '0.8-1.0']

        for s in scores.iterator(chunk_size=5000):
            total_links += 1
            if s.predicted:
                accepted_links += 1

            relation_counts[s.relation] += 1
            if s.predicted:
                relation_accepted[s.relation] += 1

            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= s.normalized_score < bucket_edges[i + 1]:
                    score_buckets[bucket_labels[i]] += 1
                    break
            else:
                if s.normalized_score >= 1.0:
                    score_buckets['0.8-1.0'] += 1

        acceptance_rate = round(accepted_links / total_links, 3) if total_links > 0 else 0.0

        relation_distribution = []
        for rel, count in relation_counts.most_common():
            acc = relation_accepted.get(rel, 0)
            relation_distribution.append({
                'relation': rel,
                'count': count,
                'accepted': acc,
                'acceptance_rate': round(acc / count, 3) if count > 0 else 0.0,
            })

        score_distribution = {
            'buckets': bucket_labels,
            'counts': [score_buckets.get(b, 0) for b in bucket_labels],
        }

        return Response({
            'country_name': country_name,
            'total_links': total_links,
            'accepted_links': accepted_links,
            'acceptance_rate': acceptance_rate,
            'relation_distribution': relation_distribution,
            'score_distribution': score_distribution,
        })


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
            country_name__iexact=country_name
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


class WorldKGPipelineRejectedSummaryView(APIView):
    """
    GET /api/worldkg-pipeline/rejected-summary/{country_name}/

    Returns summarised SpatialTripletScoreRejected data for a country:
    - Total rejected links
    - Relation distribution with counts
    - Score distribution histogram (0.2 buckets)
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name):
        from collections import Counter
        from igea.models import SpatialTripletScoreRejected

        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Primary filter: country_name (self-describing data)
        # Optional filter: snapshot_id (for specific snapshot queries)
        snapshot_id = request.query_params.get('snapshot_id')

        # Build query - always filter by country_name (also try ISO code)
        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        from django.db.models import Q
        iso_code = WorldKGPipelineService._resolve_iso_code(country_name)
        country_filter = Q(country_name__iexact=country_name)
        if iso_code and iso_code.upper() != country_name.upper():
            country_filter |= Q(country_name__iexact=iso_code)
        scores = SpatialTripletScoreRejected.objects.filter(country_filter)

        # If snapshot_id provided, also filter by it
        if snapshot_id:
            scores = scores.filter(snapshot_id=snapshot_id)
            logger.info(f"Filtering rejected by country_name={country_name} and snapshot_id={snapshot_id}")
        else:
            logger.info(f"Filtering rejected by country_name={country_name} (no snapshot_id filter)")

        logger.info(f"Query will return {scores.count()} rejected triplet scores")

        total_links = 0
        relation_counts = Counter()
        score_buckets = Counter()

        bucket_edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        bucket_labels = ['0.0-0.2', '0.2-0.4', '0.4-0.6', '0.6-0.8', '0.8-1.0']

        for s in scores.iterator(chunk_size=5000):
            total_links += 1

            relation_counts[s.relation] += 1

            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= s.normalized_score < bucket_edges[i + 1]:
                    score_buckets[bucket_labels[i]] += 1
                    break
            else:
                if s.normalized_score >= 1.0:
                    score_buckets['0.8-1.0'] += 1

        relation_distribution = []
        for rel, count in relation_counts.most_common():
            relation_distribution.append({
                'relation': rel,
                'count': count,
            })

        score_distribution = {
            'buckets': bucket_labels,
            'counts': [score_buckets.get(b, 0) for b in bucket_labels],
        }

        return Response({
            'country_name': country_name,
            'total_links': total_links,
            'relation_distribution': relation_distribution,
            'score_distribution': score_distribution,
        })


class ValidationCostEstimateView(APIView):
    """
    GET /api/worldkg-pipeline/validation-cost/{country_name}/

    Returns cost estimate for Google Places validation of rejected links.
    Works even without a configured API key — shows what it would cost.
    Includes breakdown by relation category.
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name):
        from collections import Counter
        from django.conf import settings
        from igea.models import SpatialTripletScoreRejected

        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        api_enabled = getattr(settings, 'GOOGLE_VALIDATION_ENABLED', False)
        api_key_configured = bool(getattr(settings, 'GOOGLE_API_KEY', None))

        # Resolve country name to ISO code for DB query (DB stores ISO code)
        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        iso_code = WorldKGPipelineService._resolve_iso_code(country_name)
        from django.db.models import Q
        country_filter = Q(country_name__iexact=country_name)
        if iso_code and iso_code.upper() != country_name.upper():
            country_filter |= Q(country_name__iexact=iso_code)

        rejected = SpatialTripletScoreRejected.objects.filter(country_filter)

        total_rejected = rejected.count()

        if total_rejected == 0:
            return Response({
                'country_name': country_name,
                'total_rejected': 0,
                'api_enabled': api_enabled,
                'api_key_configured': api_key_configured,
                'cost_per_call_usd': 0.005,
                'estimated_total_cost_usd': 0.0,
                'relation_breakdown': [],
                'score_breakdown': [],
                'message': 'No rejected links to validate.',
            })

        # Cost model
        cost_per_call = 0.005  # Geocoding starter tier
        estimated_total = round(total_rejected * cost_per_call, 2)

        # Breakdown by relation
        relation_counts = Counter()
        relation_score_sums = Counter()
        score_bucket_counts = Counter()

        bucket_edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        bucket_labels = ['0.0-0.2', '0.2-0.4', '0.4-0.6', '0.6-0.8', '0.8-1.0']

        for s in rejected.iterator(chunk_size=5000):
            relation_counts[s.relation] += 1
            relation_score_sums[s.relation] += s.normalized_score

            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= s.normalized_score < bucket_edges[i + 1]:
                    score_bucket_counts[bucket_labels[i]] += 1
                    break
            else:
                if s.normalized_score >= 1.0:
                    score_bucket_counts['0.8-1.0'] += 1

        # Relation breakdown with cost per category
        relation_breakdown = []
        for rel, count in relation_counts.most_common():
            avg_score = round(relation_score_sums[rel] / count, 3) if count > 0 else 0.0
            relation_breakdown.append({
                'relation': rel,
                'count': count,
                'avg_normalized_score': avg_score,
                'cost_usd': round(count * cost_per_call, 2),
                'pct_of_total': round(count / total_rejected * 100, 1),
            })

        # Score bucket breakdown
        score_breakdown = [
            {
                'bucket': bucket,
                'count': score_bucket_counts.get(bucket, 0),
                'cost_usd': round(score_bucket_counts.get(bucket, 0) * cost_per_call, 2),
            }
            for bucket in bucket_labels
        ]

        # Tiered pricing options
        pricing_tiers = [
            {
                'tier': 'Starter (Geocoding)',
                'cost_per_call': 0.005,
                'total_cost': round(total_rejected * 0.005, 2),
                'description': 'Basic geocoding validation — cheapest option',
            },
            {
                'tier': 'Advanced (Place Details)',
                'cost_per_call': 0.017,
                'total_cost': round(total_rejected * 0.017, 2),
                'description': 'Rich place details including categories and ratings',
            },
            {
                'tier': 'Premium (Place Details + Photos)',
                'cost_per_call': 0.024,
                'total_cost': round(total_rejected * 0.024, 2),
                'description': 'Full place data with photo references for visual validation',
            },
        ]

        return Response({
            'country_name': country_name,
            'total_rejected': total_rejected,
            'api_enabled': api_enabled,
            'api_key_configured': api_key_configured,
            'cost_per_call_usd': cost_per_call,
            'estimated_total_cost_usd': estimated_total,
            'relation_breakdown': relation_breakdown,
            'score_breakdown': score_breakdown,
            'pricing_tiers': pricing_tiers,
            'message': (
                f'Would validate {total_rejected:,} rejected links '
                f'at ~${estimated_total:.2f} (geocoding @ $0.005/call).'
            ) if not api_key_configured else (
                f'API configured — {total_rejected:,} links ready for validation.'
            ),
        })


# ══════════════════════════════════════════════════════════════════════════
# Planet / Continent Initialization (Async via Celery Canvas)
# ══════════════════════════════════════════════════════════════════════════


class PlanetInitializeView(APIView):
    """
    POST /api/planet/initialize/

    Triggers planet initialization + continent extraction via Celery Canvas.
    Runs Step 0 (planet init) + Step 0.5 (continent extraction) as an async chain.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from pipeline.canvas import run_planet_initialization

        planet_pbf_path = request.data.get(
            'planet_pbf_path',
        ) or request.data.get('path')

        try:
            run_id = run_planet_initialization(
                planet_pbf_path=planet_pbf_path,
                extract_continents=True,
            )
        except ImportError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ws_url = f'ws://localhost:8000/ws/pipeline/{run_id}/'

        return Response({
            'pipeline_run_id': run_id,
            'ws_url': ws_url,
            'status': 'started',
            'message': 'Planet initialization + continent extraction dispatched',
        }, status=status.HTTP_202_ACCEPTED)


class PlanetInitStatusView(APIView):
    """GET /api/planet/status/<pipeline_run_id>/"""
    permission_classes = [AllowAny]

    def get(self, request, pipeline_run_id):
        try:
            run = PipelineRun.objects.get(id=pipeline_run_id)
        except PipelineRun.DoesNotExist:
            return Response(
                {'error': 'Pipeline run not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            'pipeline_run_id': str(run.id),
            'status': run.status,
            'started_at': run.started_at.isoformat() if run.started_at else None,
            'completed_at': run.completed_at.isoformat() if run.completed_at else None,
            'error': run.error_message,
        })


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
        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        from orchestration.models import (
            CountryPipelineProfile, EligibleCountry, SnapshotJob, PipelineRun,
        )
        from django.conf import settings
        from django.utils import timezone

        country_name = request.data.get('country_name', '').strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve ISO code for the country.
        # Order: CountryPipelineProfile → _resolve_iso_code → EligibleCountry
        profile = CountryPipelineProfile.objects.filter(
            canonical_name__iexact=country_name
        ).first()
        iso = profile.iso2 if profile else None

        if not iso:
            # Try the legacy resolver (now also checks non-sovereign synthetic ISOs)
            iso = WorldKGPipelineService._resolve_iso_code(country_name)

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

        # ── SnapshotJob gate ───────────────────────────────────────────
        existing = SnapshotJob.objects.filter(
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

        ws_url = f'ws://localhost:8000/ws/pipeline/{run_id}/'

        return Response({
            'pipeline_run_id': run_id,
            'ws_url': ws_url,
            'status': 'started',
            'country_name': country_name,
            'snapshot_date': snapshot_date,
            'snapshot_job_id': str(job.id),
        }, status=status.HTTP_202_ACCEPTED)


class SnapshotDatesView(APIView):
    """
    GET /api/planet/snapshot-dates/

    Returns the snapshot date range (derived from settings) plus the set of
    dates that have at least one completed ``SnapshotJob``. Replaces the
    legacy filesystem scan of the ``continents/`` directory.

    Response shape::

        {
            "snapshot_dates": ["2025_12_31", "2024_12_31", ...],
            "completed_dates": ["2025_12_31", ...],
            "default": "2025_12_31",
            "count": 5
        }
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from django.conf import settings
        from orchestration.models import SnapshotJob

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

        return Response({
            'snapshot_dates': all_dates,
            'completed_dates': completed,
            'default': getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31'),
            'count': len(all_dates),
        })


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
        from orchestration.models import SnapshotJob

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
        from orchestration.models import SnapshotJob

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
                from api.services.views_artifact_registry import (
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
