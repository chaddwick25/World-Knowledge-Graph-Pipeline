"""Artifact Registry + Task Result REST endpoints.

Phase 3 of ``docs/plans/TEMPORAL_SHARDING_ARTIFACT_PLAN.md``:

    - ``GET /api/artifacts/``                         — list/filter PipelineAssets
    - ``GET /api/artifacts/<uuid:artifact_id>/``      — single artifact detail
    - ``GET /api/artifacts/by-stage/``                — availability-by-stage rollup
    - ``GET /api/task-results/<uuid:run_id>/``        — TaskResult rows for a run

The artifact endpoints are thin wrappers over ``ArtifactService`` which in
turn wraps ``ArtifactFacade`` (the only layer that touches the ORM). The
task-results endpoint reads ``django_celery_results.TaskResult`` joined to
``PipelineAsset`` via ``metadata.task_id`` so the frontend can show
per-task progress alongside per-stage artifact availability.

All endpoints are read-only (``GET``) and use ``AllowAny`` to match the
existing ``app-state`` endpoint's permissions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from orchestration.services.artifact_service import ArtifactService

logger = logging.getLogger(__name__)


# ── /api/artifacts/ ─────────────────────────────────────────────────────────


class ArtifactListView(APIView):
    """GET /api/artifacts/ — list PipelineAssets with optional filters.

    Query params:
        country_code   — ISO 3166-1 alpha-2/3 (shard-routes the query)
        pipeline_run_id— UUID (alternative scoping when no country_code)
        asset_type     — PipelineAsset.AssetType value (e.g. GV_TAGS_EMBEDDING)
        stage_name     — pipeline stage (e.g. embed_osm_entities)
        status         — PENDING / GENERATING / COMPLETED / FAILED
        snapshot       — snapshot_id filter (matches metadata.snapshot_id)
        subdivision    — subgraph slug filter (matches metadata.subdivision)
        limit          — cap on number of results
    """

    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params
        country_code = params.get("country_code")
        pipeline_run_id = params.get("pipeline_run_id")
        asset_type = params.get("asset_type")
        stage_name = params.get("stage_name")
        asset_status = params.get("status")
        snapshot = params.get("snapshot")
        subdivision = params.get("subdivision")
        limit = _parse_int(params.get("limit"))

        if not country_code and not pipeline_run_id:
            return Response(
                {"error": "country_code or pipeline_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            service = ArtifactService()
            rows = service.list_artifacts(
                country_code=country_code,
                pipeline_run_id=pipeline_run_id,
                asset_type=asset_type,
                stage_name=stage_name,
                status=asset_status,
                snapshot=snapshot,
                subdivision=subdivision,
                limit=limit,
            )
            return Response({"count": len(rows), "artifacts": rows})
        except Exception as exc:
            logger.error("ArtifactListView error: %s", exc, exc_info=True)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ArtifactDetailView(APIView):
    """GET /api/artifacts/<uuid:artifact_id>/ — single artifact by ID."""

    permission_classes = [AllowAny]

    def get(self, request, artifact_id: str):
        try:
            service = ArtifactService()
            row = service.get_artifact(artifact_id)
            if row is None:
                return Response(
                    {"error": f"artifact {artifact_id} not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(row)
        except Exception as exc:
            logger.error("ArtifactDetailView error: %s", exc, exc_info=True)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ArtifactAvailabilityView(APIView):
    """GET /api/artifacts/by-stage/ — data-availability rollup for the Artifacts tab.

    Query params:
        country_code    — ISO code (shard-routes the query)
        pipeline_run_id — UUID (alternative scoping)
    """

    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params
        country_code = params.get("country_code")
        pipeline_run_id = params.get("pipeline_run_id")
        if not country_code and not pipeline_run_id:
            return Response(
                {"error": "country_code or pipeline_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            service = ArtifactService()
            rows = service.availability_by_stage(
                country_code=country_code,
                pipeline_run_id=pipeline_run_id,
            )
            return Response({"count": len(rows), "availability": rows})
        except Exception as exc:
            logger.error("ArtifactAvailabilityView error: %s", exc, exc_info=True)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── /api/task-results/<run_id>/ ─────────────────────────────────────────────


class TaskResultListView(APIView):
    """GET /api/task-results/<uuid:run_id>/ — TaskResult rows for a pipeline run.

    Returns the timeline of Celery tasks linked to the run. Linking is via
    ``PipelineAsset.metadata.task_id`` (Phase 2 of the plan) and
    ``PipelineLogEntry.task_id`` — both are populated by the
    ``@pipeline_step`` decorator / ``PipelineTask.on_success`` hook.

    The response shape:
        {
          "run_id": "...",
          "run": { ... PipelineRun summary ... },
          "tasks": [ { task_id, task_name, status, date_done, worker, result, traceback }, ... ],
          "artifacts_by_task": { "<task_id>": [ <artifact handle dicts>, ... ] }
        }

    ``django_celery_results`` may be absent in minimal test envs; the import
    is lazy so the endpoint returns a clear 503 instead of crashing the whole
    API surface on import.
    """

    permission_classes = [AllowAny]

    def get(self, request, run_id: str):
        TaskResult = _import_task_result()
        if TaskResult is None:
            return Response(
                {"error": "django_celery_results is not installed"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            from orchestration.models import PipelineAsset, PipelineRun
        except Exception as exc:  # pragma: no cover
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        run = PipelineRun.objects.filter(id=run_id).first()
        if run is None:
            return Response(
                {"error": f"PipelineRun {run_id} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 1. Gather task_ids linked to this run (from PipelineLogEntry +
        #    PipelineAsset.metadata). This is the union of Celery tasks that
        #    touched the run.
        task_ids = _collect_task_ids(run_id)

        # 2. Pull TaskResult rows for those task_ids. When none are linked
        #    (older runs pre-dating Phase 2), fall back to a run-scoped query
        #    using content type / task name heuristics is not reliable, so we
        #    return an empty list rather than guess.
        tasks = []
        if task_ids:
            tasks = _serialize_task_results(TaskResult, task_ids)
        tasks.sort(key=lambda t: (t.get("date_done") or "", t.get("task_id")))

        # 3. Artifacts grouped by task_id (the join the plan describes).
        artifacts_by_task: Dict[str, List[Dict[str, Any]]] = {}
        for asset in PipelineAsset.objects.filter(pipeline_run_id=run_id):
            tid = (asset.metadata or {}).get("task_id")
            if not tid:
                continue
            country_code = run.country_code
            continent = asset.continent or ""
            handle = {
                "artifact_id": str(asset.id),
                "asset_type": asset.asset_type,
                "country_code": country_code,
                "continent": continent,
                "snapshot_date": "",
                "storage_type": asset.storage_type,
                "storage_path": asset.storage_path,
                "record_count": int(asset.record_count or 0),
                "file_size_bytes": asset.file_size_bytes,
                "stage_name": asset.stage_name or "",
                "status": asset.status,
                "metadata": dict(asset.metadata or {}),
            }
            artifacts_by_task.setdefault(tid, []).append(handle)

        run_summary = {
            "id": str(run.id),
            "country_code": run.country_code,
            "country_name": run.country_name,
            "pipeline_type": run.pipeline_type,
            "status": run.status,
            "current_stage": run.current_stage,
            "completed_stages": list(run.completed_stages or []),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }
        return Response({
            "run_id": str(run.id),
            "run": run_summary,
            "tasks": tasks,
            "artifacts_by_task": artifacts_by_task,
        })


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _import_task_result():
    """Lazily import django_celery_results.TaskResult.

    Returns the model class or ``None`` when the package is absent (so the
    endpoint can return 503 instead of crashing on import).
    """
    try:
        from django_celery_results.models import TaskResult
        return TaskResult
    except Exception as exc:
        logger.warning("django_celery_results unavailable: %s", exc)
        return None


def _collect_task_ids(run_id: str) -> List[str]:
    """Union of task_ids linked to a run via PipelineLogEntry + PipelineAsset."""
    from orchestration.models import PipelineLogEntry, PipelineAsset
    ids = set()
    for entry in PipelineLogEntry.objects.filter(pipeline_run_id=run_id):
        if entry.task_id:
            ids.add(entry.task_id)
    for asset in PipelineAsset.objects.filter(pipeline_run_id=run_id):
        tid = (asset.metadata or {}).get("task_id")
        if tid:
            ids.add(tid)
    return sorted(ids)


def _serialize_task_results(TaskResult, task_ids: List[str]) -> List[Dict[str, Any]]:
    """Serialize TaskResult rows for a list of task_ids."""
    rows = []
    for tr in TaskResult.objects.filter(task_id__in=task_ids).iterator():
        rows.append({
            "task_id": tr.task_id,
            "task_name": getattr(tr, "task_name", None),
            "status": getattr(tr, "status", None),
            "date_done": tr.date_done.isoformat() if getattr(tr, "date_done", None) else None,
            "date_created": tr.date_created.isoformat() if getattr(tr, "date_created", None) else None,
            "worker": getattr(getattr(tr, "worker", None), "name", None) if hasattr(getattr(tr, "worker", None), "name") else getattr(tr, "worker", None),
            "result": getattr(tr, "result", None),
            "traceback": getattr(tr, "traceback", None),
        })
    return rows
