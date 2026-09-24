"""TaskResult helpers shared by the durable-results endpoints.

``django_celery_results`` may be absent in minimal test envs; the import
is lazy so callers can degrade gracefully instead of crashing on import.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _import_task_result():
    """Lazily import django_celery_results.TaskResult.

    Returns the model class or ``None`` when the package is absent.
    """
    try:
        from django_celery_results.models import TaskResult
        return TaskResult
    except Exception as exc:
        logger.warning("django_celery_results unavailable: %s", exc)
        return None


def _collect_task_ids(run_id: str) -> List[str]:
    """Union of task_ids linked to a run via PipelineLogEntry + PipelineAsset."""
    from core.models import PipelineLogEntry, PipelineAsset
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
