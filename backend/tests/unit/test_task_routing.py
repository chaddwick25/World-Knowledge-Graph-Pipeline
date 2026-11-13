"""Celery routing for the GPU-bound pipeline steps.

USLP has four entry points (canvas step, subgraph fan-out, chord callback,
API batch task) and Step 5c has one. All of them must land on the k80
worker, so every name is pinned here. A rename that misses the route table
would silently move USLP back to the workstation, where it now fails the
GPU preflight.
"""

from pipeline.task_routing import GPU_ROUTED_TASK_NAMES, build_task_routes

USLP_TASK_NAMES = {
    "step_4_predict_spatial_links",
    "sub_run_subgraph_uslp",
    "step_4b_finalize_subgraph_uslp",
    "igea.tasks.run_uslp_for_subgraph_batch",
}


def test_all_uslp_entry_points_route_to_the_gpu_queue():
    routes = build_task_routes("k80-gpu")

    for name in USLP_TASK_NAMES:
        assert routes[name] == {"queue": "k80-gpu"}


def test_step_5c_is_routed_with_uslp():
    routes = build_task_routes("k80-gpu")

    assert routes["step_5c_graph_spectral_analysis"] == {"queue": "k80-gpu"}


def test_no_routes_without_a_queue():
    assert build_task_routes("") == {}
    assert build_task_routes("   ") == {}


def test_api_task_name_matches_the_route_table():
    """The API task has no explicit name, so Celery derives it from the
    module path. Pin the derived name against the route table."""
    from igea.tasks import run_uslp_for_subgraph_batch

    assert run_uslp_for_subgraph_batch.name in GPU_ROUTED_TASK_NAMES
