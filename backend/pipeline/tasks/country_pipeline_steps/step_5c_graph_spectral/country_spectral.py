"""Step 5c country-level spectral analysis (small territories path).

Extracted from ``step_5c_graph_spectral.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import logging
import os
import time
from datetime import datetime, timezone

from pipeline.tasks.helper import _log
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral._constants import (
    DEFAULT_K_EIGENVALUES,
    GRAPHML_NODE_THRESHOLD,
    LARGE_COUNTRY_K_EIGENVALUES,
    LARGE_COUNTRY_NODE_THRESHOLD,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.reporting import (
    _write_report,
)

logger = logging.getLogger("pipeline")


def _run_country_spectral_analysis(env) -> None:
    """Country-level spectral analysis (small territories — unchanged path).

    Also writes per-entity ``factor_spectral_node_metric`` rows (the
    authoritative runtime path for MapQA spectral/diffusion/community
    queries via pgvector ``<#>``) and serializes the k-NN graph to
    GraphML as a debug artifact (skipped for large graphs — see
    ``GRAPHML_NODE_THRESHOLD``).

    A JSON run report is written to ``{SPECTRAL_REPORT_DIR}/`` capturing
    solver selection, timing, graph dimensions, eigenvalue quality, and
    factor-row write status.
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

    # ── Report skeleton ──
    report = {
        "country": env.iso,
        "snapshot_date": env.snapshot_date,
        "pipeline_run_id": env.pipeline_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "stages": {},
    }

    snapshot = (
        Snapshot.objects.using('default')
        .filter(country_code__iexact=env.iso, snapshot_date=env.snapshot_date)
        .order_by('-created_at')
        .first()
    )
    if snapshot is None:
        _log(
            logger,
            "warning",
            "Step 5c: No Snapshot row — skipping spectral analysis",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            pipeline_run_id=env.pipeline_run_id,
        )
        report["status"] = "skipped_no_snapshot"
        _write_report(report, env)
        return

    # Idempotency: replace any existing fingerprint for this (region, snapshot)
    GraphSpectralFingerprint.objects.filter(region=env.iso, snapshot=snapshot).delete()

    # ── 5c.pre: Release residual memory from prior steps ──
    import gc
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
        del torch
    except ImportError:
        pass
    gc.collect()

    # ── 5c.0: Build k-NN graph ──
    _log(
        logger,
        "info",
        "Step 5c: Loading k-NN graph",
        country=env.iso,
        snapshot_date=env.snapshot_date,
        pipeline_run_id=env.pipeline_run_id,
    )
    knn = KNNGraphService()
    t_graph_start = time.time()
    G = knn.build_sparse_graph(
        country_code=env.iso, snapshot_id=env.snapshot_date,
    )
    t_graph = time.time() - t_graph_start

    n_nodes = G.n_nodes
    n_edges = len(G._canonical_edges[2])
    report["stages"]["graph_build"] = {
        "time_s": round(t_graph, 1),
        "node_count": n_nodes,
        "edge_count": n_edges,
    }

    if n_nodes < 2:
        _log(
            logger,
            "warning",
            "Step 5c: k-NN graph too small for spectral analysis — skipping",
            country=env.iso,
            node_count=n_nodes,
            pipeline_run_id=env.pipeline_run_id,
        )
        report["status"] = "skipped_graph_too_small"
        _write_report(report, env)
        return

    # ── 5c.1: Compute Laplacian eigendecomposition ──
    k = (
        LARGE_COUNTRY_K_EIGENVALUES
        if n_nodes >= LARGE_COUNTRY_NODE_THRESHOLD
        else DEFAULT_K_EIGENVALUES
    )

    _log(
        logger,
        "info",
        "Step 5c: Computing Laplacian eigendecomposition",
        country=env.iso,
        node_count=n_nodes,
        edge_count=n_edges,
        k=k,
        pipeline_run_id=env.pipeline_run_id,
    )
    spectral = SpectralAnalysisService()
    features = spectral.compute_spectral_features(G, k=k)

    solver_name = features.get("solver", "unknown")
    solve_time = features.get("solve_time", 0.0)

    report["stages"]["eigensolve"] = {
        "solver": solver_name,
        "k": k,
        "time_s": round(solve_time, 1),
        "algebraic_connectivity": features["algebraic_connectivity"],
        "spectral_gap": features["spectral_gap"],
        "eigenvalues_first5": features["eigenvalues"][:5],
        "eigenvalues_last5": features["eigenvalues"][-5:],
    }

    # ── 5c.2: Build WorldKG class signal ──
    entity_class_map = G.class_map
    signal = None
    signal_smoothness = 0.0
    if entity_class_map:
        gss = GraphSignalService()
        signal, _ = gss.build_class_signal(G, entity_class_map)
        signal_smoothness = gss.signal_smoothness(G, signal)

    report["stages"]["signal"] = {
        "has_signal": signal is not None,
        "signal_smoothness": signal_smoothness,
    }

    # ── 5c.3: Store GraphSpectralFingerprint ──
    # Truncate the Fiedler vector for storage (keep at most 10k samples)
    fiedler = features["fiedler_vector"]
    if len(fiedler) > 10_000:
        stride = len(fiedler) // 10_000
        fiedler = fiedler[::stride][:10_000]

    fp = GraphSpectralFingerprint.objects.create(
        region=env.iso,
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

    _log(
        logger,
        "info",
        "Step 5c: Stored GraphSpectralFingerprint",
        country=env.iso,
        lambda2=features["algebraic_connectivity"],
        spectral_gap=features["spectral_gap"],
        signal_smoothness=signal_smoothness,
        solver=solver_name,
        solve_time=solve_time,
        pipeline_run_id=env.pipeline_run_id,
    )

    report["stages"]["fingerprint"] = {"status": "stored", "k": k}

    # ── 5c.6: Per-node factor tables ──
    factor_status = "skipped"
    factor_rows = 0
    community_count = 0
    modularity = 0.0
    try:
        from semantic_search.services.community_detection_service import (
            CommunityDetectionService,
        )
        from semantic_search.services.factor_node_writer import FactorNodeWriter

        communities = CommunityDetectionService().detect_communities(G)
        # 0c backfill: persist the community fields on the fingerprint row.
        fp.community_count = communities["community_count"]
        fp.modularity = communities["modularity"]
        fp.save(update_fields=["community_count", "modularity"])
        factor_rows = FactorNodeWriter().write_spectral_nodes(
            G, features, env.iso, env.snapshot_date,
            node_to_community=communities["node_to_community"],
            signal=signal,
            fingerprint_id=fp.id,
        )
        community_count = communities["community_count"]
        modularity = communities["modularity"]
        factor_status = "written"
        _log(
            logger,
            "info",
            "Step 5c: Wrote SpectralNodeMetric factor rows",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            rows=factor_rows,
            community_count=community_count,
            modularity=modularity,
            pipeline_run_id=env.pipeline_run_id,
        )
    except Exception as exc:
        factor_status = f"failed: {exc}"
        _log(
            logger,
            "warning",
            "Step 5c: Factor-node write failed — pipeline continues (non-fatal)",
            country=env.iso,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )

    report["stages"]["factor_tables"] = {
        "status": factor_status,
        "rows": factor_rows,
        "community_count": community_count,
        "modularity": modularity,
    }

    # ── 5c.7: GraphML debug artifact ──
    graphml_status = "skipped_large"
    if n_nodes < GRAPHML_NODE_THRESHOLD:
        graphml_status = _serialize_graph(G.to_nx(), env.iso, env.snapshot_date)
    else:
        _log(
            logger, "info",
            "Step 5c: Skipping GraphML serialization for large graph "
            "(factor tables are authoritative at runtime)",
            country=env.iso, snapshot_date=env.snapshot_date,
            nodes=n_nodes, edges=n_edges,
            threshold=GRAPHML_NODE_THRESHOLD,
        )
    report["stages"]["graphml"] = {"status": graphml_status}

    # ── Finalize report ──
    report["status"] = "completed"
    report["total_time_s"] = round(t_graph + solve_time, 1)
    _write_report(report, env)


def _serialize_graph(G, country_code: str, snapshot_id: str) -> str:
    """Serialize the k-NN graph to GraphML as a debug artifact.

    Returns a status string: ``"written"``, ``"skipped_large"``,
    ``"skipped_no_dir"``, or ``"failed: <error>"``.
    """
    from django.conf import settings

    n_nodes = G.number_of_nodes()
    threshold = getattr(
        settings, 'GRAPHML_NODE_THRESHOLD', GRAPHML_NODE_THRESHOLD,
    )
    if n_nodes >= threshold:
        _log(
            logger, "info",
            "Step 5c: Skipping GraphML serialization for large graph "
            "(factor tables are authoritative at runtime)",
            country=country_code, snapshot_date=snapshot_id,
            nodes=n_nodes, edges=G.number_of_edges(), threshold=threshold,
        )
        return "skipped_large"

    try:
        graph_dir = getattr(settings, 'GRAPH_ARTIFACT_DIR', None)
        if not graph_dir:
            _log(
                logger, "warning",
                "Step 5c: GRAPH_ARTIFACT_DIR not configured — skipping graph serialization",
                country=country_code, snapshot_date=snapshot_id,
            )
            return "skipped_no_dir"

        os.makedirs(graph_dir, exist_ok=True)
        path = os.path.join(
            graph_dir,
            f"{country_code.lower()}_{snapshot_id}.graphml",
        )

        import networkx as nx
        nx.write_graphml(G, path)
        _log(
            logger, "info",
            "Step 5c: Serialized k-NN graph to GraphML",
            country=country_code, snapshot_date=snapshot_id,
            path=path, nodes=G.number_of_nodes(), edges=G.number_of_edges(),
        )
        return "written"
    except Exception as exc:
        _log(
            logger, "warning",
            "Step 5c: Graph serialization failed — non-fatal, graph can be rebuilt on demand",
            country=country_code, snapshot_date=snapshot_id,
            error=str(exc),
        )
        return f"failed: {exc}"
