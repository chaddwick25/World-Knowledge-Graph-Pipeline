"""Step 5c Phase A (subgraph solves) — per-subgraph spectral analysis.

Extracted from ``step_5c_graph_spectral.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import logging
import time
from datetime import datetime, timezone

import numpy as np

from pipeline.tasks.helper import _log
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral._constants import (
    DEFAULT_K_EIGENVALUES,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.reporting import (
    _write_report,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.transport_matrices import (
    _compute_transport_matrices,
)

logger = logging.getLogger("pipeline")


def _run_subgraph_spectral_analysis(env) -> None:
    """Per-subgraph spectral analysis — one GPU LOBPCG solve per subgraph.

    Each subgraph gets its own:
    - SparseGraph (built from the subgraph's polygon + buffer)
    - GraphSpectralFingerprint (region=subgraph_slug)
    - factor_spectral_node_metric rows (subgraph_slug column set)
    - Community detection
    - GraphML artifact (if small enough)
    """
    from osmsnapshot.models import Snapshot
    from semantic_search.models import GraphSpectralFingerprint
    from semantic_search.services.knn_graph_service import KNNGraphService
    from semantic_search.services.spectral_analysis_service import (
        SpectralAnalysisService,
    )
    from semantic_search.services.graph_signal_service import (
        GraphSignalService,
    )
    from semantic_search.services.community_detection_service import (
        CommunityDetectionService,
    )
    from semantic_search.services.factor_node_writer import FactorNodeWriter
    import gc

    report = {
        "country": env.iso,
        "snapshot_date": env.snapshot_date,
        "pipeline_run_id": env.pipeline_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "mode": "subgraph",
        "subgraphs": [],
    }

    snapshot = (
        Snapshot.objects.using('default')
        .filter(country_code__iexact=env.iso, snapshot_date=env.snapshot_date)
        .order_by('-created_at')
        .first()
    )
    if snapshot is None:
        _log(logger, "warning",
             "Step 5c: No Snapshot row — skipping spectral analysis",
             country=env.iso, snapshot_date=env.snapshot_date,
             pipeline_run_id=env.pipeline_run_id)
        report["status"] = "skipped_no_snapshot"
        _write_report(report, env)
        return

    knn = KNNGraphService()
    total_factor_rows = 0
    total_solve_time = 0.0
    failed_subgraphs = 0

    # Phase A: per-subgraph solves.
    # Retain eigenvectors + node_ids for Phase B (transport matrices).
    subgraph_solve_data = {}  # slug → {eigenvalues, eigenvectors, node_ids}

    for sg in env.subgraphs:
        sg_slug = sg.slug
        sg_report = {"subgraph": sg_slug, "name": sg.name, "status": "running"}

        try:
            # ── Build subgraph-scoped k-NN graph (Option A: full buffered) ──
            _log(logger, "info",
                 "Step 5c: Building subgraph k-NN graph (Option A — buffered)",
                 country=env.iso, subgraph=sg_slug,
                 pipeline_run_id=env.pipeline_run_id)

            t0 = time.time()
            G, core_ids = knn.build_sparse_graph_for_subgraph(
                country_code=env.iso,
                snapshot_id=env.snapshot_date,
                subgraph_slug=sg_slug,
                poly_path=sg.poly_path,
                bbox=(sg.bbox_min_lon, sg.bbox_min_lat,
                      sg.bbox_max_lon, sg.bbox_max_lat) if sg.bbox_min_lon else None,
                return_core_ids=True,  # Option A: full buffered graph
            )
            t_graph = time.time() - t0
            n_nodes = G.n_nodes
            n_edges = len(G._canonical_edges[2])

            sg_report["graph_build_time_s"] = round(t_graph, 1)
            sg_report["node_count"] = n_nodes
            sg_report["edge_count"] = n_edges
            sg_report["core_count"] = len(core_ids)

            if n_nodes < 2:
                _log(logger, "warning",
                     "Step 5c: Subgraph too small — skipping",
                     country=env.iso, subgraph=sg_slug, node_count=n_nodes,
                     pipeline_run_id=env.pipeline_run_id)
                sg_report["status"] = "skipped_too_small"
                report["subgraphs"].append(sg_report)
                continue

            # ── k clamp: min(k, n//20) per the subdivision plan ──
            k = min(DEFAULT_K_EIGENVALUES, max(16, n_nodes // 20))

            # ── Eigendecomposition (GPU LOBPCG — subgraphs fit in VRAM) ──
            _log(logger, "info",
                 "Step 5c: Computing eigendecomposition for subgraph",
                 country=env.iso, subgraph=sg_slug,
                 node_count=n_nodes, k=k,
                 pipeline_run_id=env.pipeline_run_id)

            spectral = SpectralAnalysisService()
            features = spectral.compute_spectral_features(G, k=k)
            solver_name = features.get("solver", "unknown")
            solve_time = features.get("solve_time", 0.0)
            total_solve_time += solve_time

            sg_report["solver"] = solver_name
            sg_report["k"] = k
            sg_report["solve_time_s"] = round(solve_time, 1)
            sg_report["algebraic_connectivity"] = features["algebraic_connectivity"]
            sg_report["spectral_gap"] = features["spectral_gap"]

            # ── Graph signal ──
            entity_class_map = G.class_map
            signal = None
            signal_smoothness = 0.0
            if entity_class_map:
                gss = GraphSignalService()
                signal, _ = gss.build_class_signal(G, entity_class_map)
                signal_smoothness = gss.signal_smoothness(G, signal)
            sg_report["signal_smoothness"] = signal_smoothness

            # ── Store fingerprint (region=subgraph_slug) ──
            GraphSpectralFingerprint.objects.filter(
                region=sg_slug, snapshot=snapshot,
            ).delete()

            fiedler = features["fiedler_vector"]
            if len(fiedler) > 10_000:
                stride = len(fiedler) // 10_000
                fiedler = fiedler[::stride][:10_000]

            fp = GraphSpectralFingerprint.objects.create(
                region=sg_slug,
                snapshot=snapshot,
                eigenvalues=features["eigenvalues"],
                fiedler_vector=fiedler,
                algebraic_connectivity=features["algebraic_connectivity"],
                spectral_gap=features["spectral_gap"],
                signal_smoothness=signal_smoothness,
                node_count=features["node_count"],
                edge_count=features["edge_count"],
                k_eigenvalues=k,
            )

            # ── Community detection + factor rows ──
            # core_ids prunes buffer-only entities at write time (Option A).
            # fingerprint_id links the loadings to THIS eigenbasis (Phase 1
            # coherence check at runtime).  The fingerprint row gets the
            # community fields persisted (0c backfill).
            communities = CommunityDetectionService().detect_communities(G)
            fp.community_count = communities["community_count"]
            fp.modularity = communities["modularity"]
            fp.save(update_fields=["community_count", "modularity"])
            factor_rows = FactorNodeWriter().write_spectral_nodes(
                G, features, env.iso, env.snapshot_date,
                node_to_community=communities["node_to_community"],
                signal=signal,
                subgraph_slug=sg_slug,
                core_ids=core_ids,
                fingerprint_id=fp.id,
            )
            total_factor_rows += factor_rows

            sg_report["factor_rows"] = factor_rows
            sg_report["community_count"] = communities["community_count"]
            sg_report["modularity"] = communities["modularity"]
            sg_report["status"] = "completed"

            # Retain for Phase B (transport matrix computation)
            subgraph_solve_data[sg_slug] = {
                "eigenvalues": np.asarray(features["eigenvalues"], dtype=float),
                "eigenvectors": np.asarray(features["eigenvectors"], dtype=float),
                "node_ids": np.asarray(features["node_order"], dtype=np.int64),
            }

            _log(logger, "info",
                 "Step 5c: Subgraph complete",
                 country=env.iso, subgraph=sg_slug,
                 solver=solver_name, solve_time=solve_time,
                 factor_rows=factor_rows,
                 community_count=communities["community_count"],
                 pipeline_run_id=env.pipeline_run_id)

        except Exception as exc:
            failed_subgraphs += 1
            sg_report["status"] = f"failed: {exc}"
            _log(logger, "warning",
                 "Step 5c: Subgraph failed — continuing (non-fatal)",
                 country=env.iso, subgraph=sg_slug, error=str(exc),
                 pipeline_run_id=env.pipeline_run_id)

        report["subgraphs"].append(sg_report)

        # Release memory between subgraphs (aggressive — prevents VRAM
        # fragmentation accumulation across 30+ sequential GPU solves).
        # USLP avoids this by running each subgraph in a separate
        # call_command process; Step 5c runs all in one process so we
        # must manually flush the GPU cache and Python refs.
        try:
            import torch
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
                torch.cuda.empty_cache()  # twice: first frees cached
                                           # blocks, second defrags
            del torch
        except ImportError:
            pass
        gc.collect()

    # ── Phase B: Transport matrix computation (functional maps) ──
    transport_report = _compute_transport_matrices(env, subgraph_solve_data)
    report["transport"] = transport_report

    report["status"] = "completed" if failed_subgraphs == 0 else "completed_with_failures"
    report["total_factor_rows"] = total_factor_rows
    report["total_solve_time_s"] = round(total_solve_time, 1)
    report["failed_subgraphs"] = failed_subgraphs
    _write_report(report, env)

    _log(logger, "info",
         "Step 5c: All subgraphs processed",
         country=env.iso, subgraph_count=len(env.subgraphs),
         failed=failed_subgraphs, total_factor_rows=total_factor_rows,
         pipeline_run_id=env.pipeline_run_id)
