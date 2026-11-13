"""Celery task routing for GPU-bound pipeline steps.

USLP (Step 4, its subgraph fan-out, its chord callback, and the API batch
task) and Step 5c run on the k80 GPU worker. Routing is by task name, so
every entry point (full canvas, ``run_pipeline --stage``, API dispatch)
lands on the same queue.

The queue is opt-in: an empty queue name returns no routes, which keeps a
single-host dev stack working. The deployment sets ``K80_GPU_QUEUE=k80-gpu``
on the backend and worker services (docker-compose.yml).
"""

GPU_ROUTED_TASK_NAMES = (
    # Step 4 (USLP)
    "step_4_predict_spatial_links",
    "sub_run_subgraph_uslp",
    "step_4b_finalize_subgraph_uslp",
    # API-triggered USLP batch (igea/views.py -> igea/tasks.py)
    "igea.tasks.run_uslp_for_subgraph_batch",
    # Step 5c (graph spectral)
    "step_5c_graph_spectral_analysis",
)


def build_task_routes(gpu_queue: str) -> dict:
    """Return ``CELERY_TASK_ROUTES`` for the GPU steps, or ``{}`` when unset."""
    queue = (gpu_queue or "").strip()
    if not queue:
        return {}
    return {name: {"queue": queue} for name in GPU_ROUTED_TASK_NAMES}
