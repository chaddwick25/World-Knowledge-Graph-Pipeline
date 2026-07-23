"""
Application State API View.

Provides a single endpoint that replaces multiple older endpoints
for pipeline progress and country status.

GET /api/app-state/{country_name}/
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from api.services.app_state_service import AppStateService

logger = logging.getLogger(__name__)


class AppStateView(APIView):
    """
    GET /api/app-state/{country_name}/

    Returns consolidated application state for a country.
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        if not country_name or not country_name.strip():
            return Response(
                {"error": "country_name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            service = AppStateService()
            state = service.get_state(country_name.strip())
            pipeline_steps = service.get_pipeline_steps(state.pipeline)

            response = {
                "country_name": state.country_name,
                "iso": state.iso,
                "pipeline": {
                    "status": state.pipeline.status if state.pipeline else None,
                    "run_id": state.pipeline.run_id if state.pipeline else None,
                    "pipeline_type": state.pipeline.pipeline_type if state.pipeline else None,
                    "current_stage": state.pipeline.current_stage if state.pipeline else None,
                    "completed_stages": state.pipeline.completed_stages if state.pipeline else [],
                    "stage_metrics": state.pipeline.stage_metrics if state.pipeline else {},
                    "queued_at": state.pipeline.queued_at if state.pipeline else None,
                    "started_at": state.pipeline.started_at if state.pipeline else None,
                    "completed_at": state.pipeline.completed_at if state.pipeline else None,
                    "error_message": state.pipeline.error_message if state.pipeline else None,
                    "celery_state": state.pipeline.celery_state if state.pipeline else None,
                    "is_zombie": state.pipeline.is_zombie if state.pipeline else None,
                    "subgraphs": [
                        {
                            "slug": sg.slug,
                            "name": sg.name,
                            "status": sg.status,
                            "celery_state": sg.celery_state,
                        }
                        for sg in (state.pipeline.subgraphs if state.pipeline else [])
                    ],
                } if state.pipeline else None,
                "search": {
                    "is_ready": state.search.is_ready,
                    "entity_count": state.search.entity_count,
                    "has_db_embeddings": state.search.has_db_embeddings,
                },
                "capabilities": state.capabilities,
                "pipeline_steps": pipeline_steps,
            }

            return Response(response)

        except Exception as exc:
            logger.error(f"AppState error for {country_name}: {exc}", exc_info=True)
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
