"""Step 5c Phase B — functional-map transport matrices.

Extracted from ``step_5c_graph_spectral.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import logging

import numpy as np

from pipeline.tasks.helper import _log

logger = logging.getLogger("pipeline")


def _compute_transport_matrices(
    env,
    subgraph_solve_data: dict,
) -> dict:
    """Phase B: compute functional map matrices between adjacent subgraph pairs.

    Uses shared buffer-zone entities (Option A — entities in both subgraphs'
    buffered graphs) as correspondence points to fit a k×k transport matrix
    per adjacent pair.  See ``docs/plans/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md``.

    Args:
        env: country envelope with subgraph configs
        subgraph_solve_data: {slug → {eigenvalues, eigenvectors, node_ids}}
            retained from Phase A solves

    Returns:
        Report dict with computed count, residuals, and per-pair details.
    """
    from semantic_search.services.subgraph_transport_service import (
        SubgraphTransportService,
    )

    report = {
        "status": "skipped",
        "pairs_total": 0,
        "pairs_computed": 0,
        "pairs_skipped": 0,
        "mean_fit_residual": None,
        "mean_commutativity_residual": None,
        "min_shared_count": None,
        "details": [],
    }

    if len(subgraph_solve_data) < 2:
        _log(logger, "info",
             "Step 5c Phase B: < 2 subgraphs solved — skipping transport",
             country=env.iso,
             pipeline_run_id=env.pipeline_run_id)
        return report

    transport_svc = SubgraphTransportService()

    # Determine adjacent subgraph pairs
    try:
        adjacency = transport_svc.compute_adjacency(list(env.subgraphs))
    except Exception as exc:
        _log(logger, "warning",
             "Step 5c Phase B: adjacency computation failed — skipping",
             country=env.iso, error=str(exc),
             pipeline_run_id=env.pipeline_run_id)
        report["status"] = f"failed: {exc}"
        return report

    report["pairs_total"] = len(adjacency)
    if not adjacency:
        _log(logger, "info",
             "Step 5c Phase B: no adjacent subgraph pairs — skipping",
             country=env.iso,
             pipeline_run_id=env.pipeline_run_id)
        report["status"] = "no_adjacent_pairs"
        return report

    _log(logger, "info",
         f"Step 5c Phase B: computing transport matrices for {len(adjacency)} adjacent pairs",
         country=env.iso, pair_count=len(adjacency),
         pipeline_run_id=env.pipeline_run_id)

    transport_data = []
    fit_residuals = []
    commutativity_residuals = []
    shared_counts = []

    for slug_a, slug_b in adjacency:
        # Both subgraphs must have been solved successfully in Phase A
        if slug_a not in subgraph_solve_data or slug_b not in subgraph_solve_data:
            report["pairs_skipped"] += 1
            report["details"].append({
                "pair": f"{slug_a}→{slug_b}",
                "status": "skipped_no_solve",
            })
            continue

        data_a = subgraph_solve_data[slug_a]
        data_b = subgraph_solve_data[slug_b]

        try:
            # Shared entities: in both buffered graphs (Option A)
            node_ids_a = set(int(oid) for oid in data_a["node_ids"])
            node_ids_b = set(int(oid) for oid in data_b["node_ids"])
            shared_ids = np.array(sorted(node_ids_a & node_ids_b), dtype=np.int64)

            result = transport_svc.compute_transport_matrix(
                eigenvalues_a=data_a["eigenvalues"],
                eigenvectors_a=data_a["eigenvectors"],
                node_ids_a=data_a["node_ids"],
                eigenvalues_b=data_b["eigenvalues"],
                eigenvectors_b=data_b["eigenvectors"],
                node_ids_b=data_b["node_ids"],
                shared_node_ids=shared_ids,
            )

            if result is None:
                report["pairs_skipped"] += 1
                report["details"].append({
                    "pair": f"{slug_a}→{slug_b}",
                    "status": "skipped_insufficient_shared",
                })
                continue

            transport_data.append({
                "subgraph_from": slug_a,
                "subgraph_to": slug_b,
                "transport_matrix": result["transport_matrix"],
                "k_dim": result["k_dim"],
                "shared_entity_count": result["shared_entity_count"],
                "fit_residual": result["fit_residual"],
                "commutativity_residual": result["commutativity_residual"],
            })

            fit_residuals.append(result["fit_residual"])
            commutativity_residuals.append(result["commutativity_residual"])
            shared_counts.append(result["shared_entity_count"])

            report["pairs_computed"] += 1
            report["details"].append({
                "pair": f"{slug_a}→{slug_b}",
                "status": "computed",
                "shared_count": result["shared_entity_count"],
                "fit_residual": round(result["fit_residual"], 4),
                "commutativity_residual": round(result["commutativity_residual"], 4),
            })

        except Exception as exc:
            report["pairs_skipped"] += 1
            report["details"].append({
                "pair": f"{slug_a}→{slug_b}",
                "status": f"failed: {exc}",
            })
            _log(logger, "warning",
                 "Step 5c Phase B: pair failed — continuing",
                 country=env.iso, pair=f"{slug_a}→{slug_b}",
                 error=str(exc),
                 pipeline_run_id=env.pipeline_run_id)

    # Batch-write all transport matrices
    if transport_data:
        try:
            written = transport_svc.write_transport_matrices(
                snapshot_id=env.snapshot_date,
                country_code=env.iso,
                transport_data=transport_data,
            )
            report["rows_written"] = written
            report["status"] = "completed"
        except Exception as exc:
            report["status"] = f"write_failed: {exc}"
            _log(logger, "warning",
                 "Step 5c Phase B: batch write failed",
                 country=env.iso, error=str(exc),
                 pipeline_run_id=env.pipeline_run_id)
    else:
        report["status"] = "no_matrices_computed"

    if fit_residuals:
        report["mean_fit_residual"] = round(
            sum(fit_residuals) / len(fit_residuals), 4,
        )
        report["mean_commutativity_residual"] = round(
            sum(commutativity_residuals) / len(commutativity_residuals), 4,
        )
        report["min_shared_count"] = min(shared_counts)

    _log(logger, "info",
         "Step 5c Phase B: complete",
         country=env.iso,
         pairs_computed=report["pairs_computed"],
         pairs_skipped=report["pairs_skipped"],
         mean_fit_residual=report["mean_fit_residual"],
         min_shared_count=report["min_shared_count"],
         pipeline_run_id=env.pipeline_run_id)

    return report
