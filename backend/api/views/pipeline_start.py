"""POST /api/worldkg-pipeline-v2/start/
    Payload: { "country_name": "Jamaica", "snapshot_date": "<optional>",
               "skip_entropy_gate": <bool>, "skip_enrich": <bool>, "force": <bool> }
    Returns: { "pipeline_run_id": "<uuid>", "ws_url": "ws://..." }
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from core.services.pipeline.run_dispatch_service import (
    PipelineDispatchError,
    dispatch_country_pipeline,
)

logger = logging.getLogger(__name__)


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
        from django.conf import settings

        try:
            result = dispatch_country_pipeline(
                country_name=request.data.get('country_name', ''),
                snapshot_date=request.data.get('snapshot_date') or None,
                skip_entropy_gate=request.data.get('skip_entropy_gate', False),
                skip_enrich=request.data.get('skip_enrich', False),
                force=request.data.get('force', False),
            )
        except PipelineDispatchError as exc:
            http_status = {
                'invalid': status.HTTP_400_BAD_REQUEST,
                'conflict': status.HTTP_409_CONFLICT,
                'unavailable': status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.code, status.HTTP_400_BAD_REQUEST)
            return Response({'error': str(exc)}, status=http_status)

        run_id = result['pipeline_run_id']
        ws_url = f'ws://{settings.WS_HOST}:{settings.WS_PORT}/ws/pipeline/{run_id}/'

        return Response({
            'pipeline_run_id': run_id,
            'ws_url': ws_url,
            'status': 'started',
            'country_name': result['country_name'],
            'snapshot_date': result['snapshot_date'],
            'snapshot_job_id': result['snapshot_job_id'],
        }, status=status.HTTP_202_ACCEPTED)


# NOTE: Planet initialization (formerly steps 0a–0m) is a Docker startup
# step — ``python manage.py init_planet`` via docker-entrypoint.sh. The
# legacy POST /planet/initialize/ and GET /planet/status/<uuid>/ facades
# were removed; readiness is served by GET /api/system/status/
# (SystemStatusView) and derived from the latest finalized PlanetSnapshot.
