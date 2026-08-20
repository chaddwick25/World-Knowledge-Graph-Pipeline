"""Celery task: Step 5c — Graph & Spectral Analysis.

Computes Laplacian spectral features on the k-NN graph built in Step 5,
encodes WorldKG classes as graph signals, and stores the
``GraphSpectralFingerprint``.

This step is **non-fatal**: if spectral analysis fails, the pipeline
continues to Step 5d / Step 6. Spectral features are optional for search
— they enhance temporal queries and community detection but are not
required for basic spatial search.

A **run report** is generated at the end of each Step 5c execution and
saved alongside the pipeline logs. The report captures which solver was
used (GPU LOBPCG, CPU shift-invert, or CPU eigsh), timing, graph
dimensions, eigenvalue quality, and factor-row write status — enabling
quick comparison across countries and snapshots without parsing logs.

References:
- [COHEN:Ch13] — Eigendecomposition
- [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
- [DMLS:Ch3] — Batch processing (pre-compute expensive steps)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np

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
# Skip GraphML serialization for graphs above this node count.
# ``nx.write_graphml`` builds the full ``xml.etree.ElementTree`` in memory
# (one Python ``Element`` object per node/edge/attribute — ~100+ bytes each
# before character data), which for 73.8M edges allocates 20–40 GB on top of
# the NetworkX graph.  GraphML is a debug artifact only — the factor-table
# path (``factor_spectral_node_metric`` + pgvector ``<#>``) is authoritative
# at runtime.  Tunable via the ``GRAPHML_NODE_THRESHOLD`` Django setting.
GRAPHML_NODE_THRESHOLD = 200_000

# Node-count threshold for routing between country-level GPU LOBPCG and
# subdivision-scoped spectral analysis.  Below this threshold, the country
# graph fits in GPU VRAM and is solved as one global eigenbasis (fastest
# path — one solve, one eigenbasis, no transport matrices needed).  At or
# above this threshold, the country is split into subgraphs, each solved
# independently, with functional-map transport matrices computed in
# Phase B for cross-subgraph diffusion at runtime.
#
# GPU LOBPCG VRAM at k=128, float32: ~2240 bytes/node.  16 GB VRAM minus
# ~2 GB torch overhead ≈ 14 GB available → ~6.25M nodes theoretical max,
# ~5M with safety margin for fragmentation and convergence spikes.
# IE (9.7M nodes at current ingestion limits) uses subdivision; JM/BZ/CV
# use country-level GPU.  The 2.47M IE run (2026-08-19) was before the
# uslp.limit raise from 200K to 1.72M which grew IE to 9.7M entities.
SUBDIVISION_NODE_THRESHOLD = 5_000_000


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
    """Compute + store spectral features for ``env``.

    Routing protocol (SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md):

    1. **< SUBDIVISION_NODE_THRESHOLD (5M) nodes** → country-level GPU
       LOBPCG.  One global eigenbasis, one solve, no transport matrices.
       Fastest path — the country graph fits in GPU VRAM.

    2. **≥ SUBDIVISION_NODE_THRESHOLD nodes** → subdivision-scoped
       spectral analysis.  Each subgraph is solved independently on GPU
       LOBPCG, then functional-map transport matrices are computed in
       Phase B for cross-subgraph diffusion at runtime.

    3. **≥ threshold but no subgraphs configured** → falls back to the
       country-level path (which will use the CPU shift-invert solver
       for large graphs).  Logs a warning that subgraphs should be
       configured for countries at this scale.

    A JSON run report is written to ``{SPECTRAL_REPORT_DIR}/``.
    """
    import dataclasses

    # Rehydrate subgraphs from DB (same pattern as Step 5)
    if not env.has_subgraphs or not env.subgraphs:
        try:
            fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
            if fresh.has_subgraphs and fresh.subgraphs:
                env = dataclasses.replace(
                    env,
                    subgraphs=fresh.subgraphs,
                    state=dataclasses.replace(env.state, has_subgraphs=True),
                )
        except Exception:
            pass

    # ── Node-count-based routing ──
    # Count OsmEntity rows for this country/snapshot to decide whether
    # to use the country-level GPU path or the subdivision path.
    node_count = _count_country_nodes(env.iso, env.snapshot_date)

    if node_count is not None and node_count < SUBDIVISION_NODE_THRESHOLD:
        _log(logger, "info",
             "Step 5c: Country has %d nodes (< %d threshold) — "
             "using country-level GPU LOBPCG (one global eigenbasis)",
             country=env.iso, node_count=node_count,
             threshold=SUBDIVISION_NODE_THRESHOLD,
             pipeline_run_id=env.pipeline_run_id)
        _run_country_spectral_analysis(env)
        return

    if node_count is not None and node_count >= SUBDIVISION_NODE_THRESHOLD:
        if env.has_subgraphs and env.subgraphs:
            _log(logger, "info",
                 "Step 5c: Country has %d nodes (≥ %d threshold) — "
                 "using subdivision spectral analysis + functional maps",
                 country=env.iso, node_count=node_count,
                 threshold=SUBDIVISION_NODE_THRESHOLD,
                 subgraph_count=len(env.subgraphs),
                 pipeline_run_id=env.pipeline_run_id)
            _run_subgraph_spectral_analysis(env)
            return
        else:
            _log(logger, "warning",
                 "Step 5c: Country has %d nodes (≥ %d threshold) but no "
                 "subgraphs configured — falling back to country-level path "
                 "(subgraphs should be configured for countries at this scale)",
                 country=env.iso, node_count=node_count,
                 threshold=SUBDIVISION_NODE_THRESHOLD,
                 pipeline_run_id=env.pipeline_run_id)

    # Fallback: if node count couldn't be determined, use the legacy
    # has_subgraphs routing
    if env.has_subgraphs and env.subgraphs:
        _run_subgraph_spectral_analysis(env)
    else:
        _run_country_spectral_analysis(env)


def _count_country_nodes(country_code: str, snapshot_id: str):
    """Count OsmEntity rows for a country/snapshot.

    Returns the count, or None if the query fails (caller falls back to
    legacy has_subgraphs routing).
    """
    try:
        from worldkg_nca.models import OsmEntity
        return (
            OsmEntity.objects.using("vectors")
            .filter(
                country_code__iexact=country_code,
                snapshot_id=snapshot_id,
            )
            .count()
        )
    except Exception as exc:
        _log(logger, "warning",
             "Step 5c: Failed to count country nodes — using legacy routing",
             country=country_code, error=str(exc))
        return None


def _compute_transport_matrices(
    env: CountryEnvelope,
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


def _run_subgraph_spectral_analysis(env: CountryEnvelope) -> None:
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

            GraphSpectralFingerprint.objects.create(
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
            # core_ids prunes buffer-only entities at write time (Option A)
            communities = CommunityDetectionService().detect_communities(G)
            factor_rows = FactorNodeWriter().write_spectral_nodes(
                G, features, env.iso, env.snapshot_date,
                node_to_community=communities["node_to_community"],
                signal=signal,
                subgraph_slug=sg_slug,
                core_ids=core_ids,
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


def _run_country_spectral_analysis(env: CountryEnvelope) -> None:
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
        factor_rows = FactorNodeWriter().write_spectral_nodes(
            G, features, env.iso, env.snapshot_date,
            node_to_community=communities["node_to_community"],
            signal=signal,
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


def _write_report(report: dict, env: CountryEnvelope) -> None:
    """Write the Step 5c run report as a JSON file.

    Reports are saved to ``{SPECTRAL_REPORT_DIR}/{country}_{snapshot}_{run_id}.json``.
    If ``SPECTRAL_REPORT_DIR`` is not configured, the report is logged
    but not written to disk.
    """
    try:
        from django.conf import settings
        report_dir = getattr(settings, 'SPECTRAL_REPORT_DIR', None)
        if not report_dir:
            # Log the report as structured info
            _log(
                logger, "info",
                "Step 5c: Run report (SPECTRAL_REPORT_DIR not configured)",
                country=env.iso,
                snapshot_date=env.snapshot_date,
                pipeline_run_id=env.pipeline_run_id,
                report=json.dumps(report, indent=2),
            )
            return

        os.makedirs(report_dir, exist_ok=True)
        run_id = env.pipeline_run_id or "norunid"
        filename = f"step5c_{env.iso.lower()}_{env.snapshot_date}_{run_id}.json"
        path = os.path.join(report_dir, filename)
        with open(path, "w") as f:
            json.dump(report, f, indent=2)

        _log(
            logger, "info",
            "Step 5c: Run report written",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            pipeline_run_id=env.pipeline_run_id,
            path=path,
            status=report["status"],
            solver=report.get("stages", {}).get("eigensolve", {}).get("solver", "n/a"),
        )
    except Exception as exc:
        _log(
            logger, "warning",
            "Step 5c: Failed to write run report — non-fatal",
            country=env.iso,
            error=str(exc),
        )
