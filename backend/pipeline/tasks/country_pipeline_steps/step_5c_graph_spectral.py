"""Celery task: Step 5c — Graph & Spectral Analysis.

Computes Laplacian spectral features on the k-NN graph built in Step 5,
encodes WorldKG classes as graph signals, and stores the
``GraphSpectralFingerprint``.

This step is **non-fatal**: if spectral analysis fails, the pipeline
continues to Step 5d / Step 6. Spectral features are optional for search
— they enhance temporal queries and community detection but are not
required for basic spatial search.

References:
- [COHEN:Ch13] — Eigendecomposition
- [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
- [DMLS:Ch3] — Batch processing (pre-compute expensive steps)
"""

from __future__ import annotations

import logging

from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

# Default number of eigenvalues to compute. Large countries (Norway ~1-3M
# nodes) may need k=64 — eigsh with k=128 takes ~30-60 min for 3M nodes.
DEFAULT_K_EIGENVALUES = 128
LARGE_COUNTRY_K_EIGENVALUES = 64
# Country node-count threshold above which we reduce k to keep eigsh tractable.
LARGE_COUNTRY_NODE_THRESHOLD = 500_000


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_5c_graph_spectral_analysis",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("graph_spectral_analysis", CountryEnvelope, 5.7)
def step_5c_graph_spectral_analysis(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 5c: Compute spectral features on the k-NN graph.

    Pipeline:
      5c.1  Load k-NN graph from Step 5 (KNNGraphService.build_graph)
      5c.2  Compute Laplacian eigendecomposition (k=128, or k=64 for large)
      5c.3  Build WorldKG class signal on graph (GraphSignalService)
      5c.4  Compute signal smoothness s^T L s
      5c.5  Store GraphSpectralFingerprint

    Non-fatal: failures are logged and the pipeline continues.
    """
    try:
        _run_spectral_analysis(env)
    except Exception as exc:
        _log(
            logger,
            "warning",
            "Step 5c: Spectral analysis failed — pipeline continues (non-fatal)",
            country=env.iso,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )

    _log(
        logger,
        "info",
        "Step 5c complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


def _run_spectral_analysis(env: CountryEnvelope) -> None:
    """Compute + store the GraphSpectralFingerprint for ``env``.

    Also serializes the k-NN graph to GraphML so that query-time
    operators (heat kernel diffusion, Dijkstra, BFS) can load it
    without rebuilding.  This follows the Spatial-Agent paper's model:
    the graph is the computational manifold on which operators act.
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
        return

    # Idempotency: replace any existing fingerprint for this (region, snapshot)
    GraphSpectralFingerprint.objects.filter(region=env.iso, snapshot=snapshot).delete()

    _log(
        logger,
        "info",
        "Step 5c: Loading k-NN graph",
        country=env.iso,
        snapshot_date=env.snapshot_date,
        pipeline_run_id=env.pipeline_run_id,
    )
    knn = KNNGraphService()
    G = knn.build_graph(country_code=env.iso, snapshot_id=env.snapshot_date)

    n_nodes = G.number_of_nodes()
    if n_nodes < 2:
        _log(
            logger,
            "warning",
            "Step 5c: k-NN graph too small for spectral analysis — skipping",
            country=env.iso,
            node_count=n_nodes,
            pipeline_run_id=env.pipeline_run_id,
        )
        return

    # ── 5c.0: Serialize the k-NN graph to GraphML ──
    # This enables query-time graph operators (heat kernel diffusion,
    # Dijkstra shortest path, BFS) without rebuilding the graph.
    # The graph is the pipeline artifact; the spectral fingerprint is
    # the summary.  Both are produced from the same build_graph() call.
    _serialize_graph(G, env.iso, env.snapshot_date)

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
        edge_count=G.number_of_edges(),
        k=k,
        pipeline_run_id=env.pipeline_run_id,
    )
    spectral = SpectralAnalysisService()
    features = spectral.compute_spectral_features(G, k=k)

    # Build the wkg_class signal from node attributes populated by build_graph
    entity_class_map = {
        node: data.get('wkg_class')
        for node, data in G.nodes(data=True)
        if data.get('wkg_class')
    }
    signal = None
    signal_smoothness = 0.0
    if entity_class_map:
        gss = GraphSignalService()
        signal, _ = gss.build_class_signal(G, entity_class_map)
        signal_smoothness = gss.signal_smoothness(G, signal)

    # Truncate the Fiedler vector for storage (keep at most 10k samples)
    fiedler = features["fiedler_vector"]
    if len(fiedler) > 10_000:
        # Even stride sampling
        stride = len(fiedler) // 10_000
        fiedler = fiedler[::stride][:10_000]

    GraphSpectralFingerprint.objects.create(
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
        pipeline_run_id=env.pipeline_run_id,
    )

    # ── 5c.6: Per-node factor tables (FACTOR_NODE_RUNTIME_JOINS_PLAN.md) ──
    # Flush per-entity spectral/structural metrics into the flat
    # ``factor_spectral_node_metric`` table so the runtime executor
    # resolves SUPPORT/factor nodes via SQL joins instead of loading
    # the GraphML artifact + NetworkX + scipy at request time.
    # Non-fatal: the region-level fingerprint above is the primary output.
    try:
        from semantic_search.services.community_detection_service import (
            CommunityDetectionService,
        )
        from semantic_search.services.factor_node_writer import FactorNodeWriter

        communities = CommunityDetectionService().detect_communities(G)
        n_rows = FactorNodeWriter().write_spectral_nodes(
            G, features, env.iso, env.snapshot_date,
            node_to_community=communities["node_to_community"],
            signal=signal,
        )
        _log(
            logger,
            "info",
            "Step 5c: Wrote SpectralNodeMetric factor rows",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            rows=n_rows,
            community_count=communities["community_count"],
            modularity=communities["modularity"],
            pipeline_run_id=env.pipeline_run_id,
        )
    except Exception as exc:
        _log(
            logger,
            "warning",
            "Step 5c: Factor-node write failed — pipeline continues (non-fatal)",
            country=env.iso,
            error=str(exc),
            pipeline_run_id=env.pipeline_run_id,
        )


def _serialize_graph(G, country_code: str, snapshot_id: str) -> None:
    """Serialize the k-NN graph to GraphML for query-time operators.

    The graph is written to ``{GRAPH_ARTIFACT_DIR}/{country}_{snapshot}.graphml``.
    Node attributes (``wkg_class``) and edge weights are preserved.

    This is non-fatal: if serialization fails, the pipeline continues.
    The graph can still be rebuilt on-demand at query time.
    """
    import os
    from django.conf import settings

    try:
        graph_dir = getattr(settings, 'GRAPH_ARTIFACT_DIR', None)
        if not graph_dir:
            _log(
                logger, "warning",
                "Step 5c: GRAPH_ARTIFACT_DIR not configured — skipping graph serialization",
                country=country_code, snapshot_date=snapshot_id,
            )
            return

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
    except Exception as exc:
        _log(
            logger, "warning",
            "Step 5c: Graph serialization failed — non-fatal, graph can be rebuilt on demand",
            country=country_code, snapshot_date=snapshot_id,
            error=str(exc),
        )
