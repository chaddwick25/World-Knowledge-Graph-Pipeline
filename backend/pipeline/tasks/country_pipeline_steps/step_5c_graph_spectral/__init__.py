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

Monolith split (Phase 5 of PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN):
    The phase bodies moved to package submodules — ``country_spectral.py``,
    ``subgraph_spectral.py``, ``transport_matrices.py``, ``reporting.py``
    (constants in ``_constants.py``).  This module keeps the @pipeline_step
    task, the routing dispatcher, and re-exports the phase functions so
    existing import sites keep working.
"""

import dataclasses
import logging

from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral._constants import (
    DEFAULT_K_EIGENVALUES,
    GRAPHML_NODE_THRESHOLD,
    LARGE_COUNTRY_K_EIGENVALUES,
    LARGE_COUNTRY_NODE_THRESHOLD,
    SUBDIVISION_NODE_THRESHOLD,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.country_spectral import (
    _run_country_spectral_analysis,
    _serialize_graph,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.reporting import (
    _write_report,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.subgraph_spectral import (
    _run_subgraph_spectral_analysis,
)
from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral.transport_matrices import (
    _compute_transport_matrices,
)

logger = logging.getLogger("pipeline")


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
