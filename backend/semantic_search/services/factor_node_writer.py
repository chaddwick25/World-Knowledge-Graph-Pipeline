"""FactorNodeWriter — write per-entity factor-node metric tables (batch side).

Implements the batch half of docs/plans/FACTOR_NODE_RUNTIME_JOINS_PLAN.md:
Step 5c/5d compute graph/spectral metrics in Celery; this service flushes
them into the flat, join-friendly ``factor_*`` tables on the ``vectors`` DB
so the runtime executor resolves SUPPORT/factor nodes via SQL joins instead
of GraphML + NetworkX + scipy at request time.

In Spatial-Agent terms (§3.3): each row is a materialized factor node of the
factorized operator–concept hypergraph G′.

Write pattern: delete-then-insert per (snapshot, country) — same idempotency
pattern as ``GraphSpectralFingerprint`` in step_5c.  Uses ORM bulk_create in
batches; the tables start empty per snapshot so there is no upsert merge.

References:
- [DMLS:Ch3] — Batch processing (pre-compute expensive steps)
- [COHEN:Ch13] — Eigendecomposition
- [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
"""

from __future__ import annotations

import logging

import networkx as nx
import numpy as np

from worldkg_nca.models import (
    EIGEN_LOADING_DIM,
    DriftNodeMetric,
    SpectralNodeMetric,
)

logger = logging.getLogger(__name__)

BULK_BATCH_SIZE = 5000


class FactorNodeWriter:
    """Write per-node spectral/structural/drift rows to the factor tables."""

    # ── Step 5c: spectral + structural ──────────────────────────────────

    # Above this node count, per-node clustering coefficients are skipped
    # (stored as NULL).  ``nx.clustering`` is O(Σ deg_i²) and requires the
    # NetworkX graph; a sparse A² diagonal computation for k=50 k-NN
    # graphs would need ~6B multiply-adds.  The field is nullable and not
    # used by any runtime ranking query (FactorResolutionService fetches
    # it for display only).
    CLUSTERING_NODE_THRESHOLD = 200_000

    def write_spectral_nodes(
        self,
        G,
        features: dict,
        country_code: str,
        snapshot_id: str,
        node_to_community: dict = None,
        signal: np.ndarray = None,
        subgraph_slug: str = None,
        core_ids: set = None,
    ) -> int:
        """Write one SpectralNodeMetric row per graph node.

        Args:
            G: k-NN graph (undirected; node keyspace is osm_id) —
               ``networkx.Graph`` (small graphs/tests) or ``SparseGraph``
               (large graphs — COO path, no NetworkX materialisation)
            features: return value of
                ``SpectralAnalysisService.compute_spectral_features`` —
                must include ``eigenvectors`` (N×K) and ``node_order``
            country_code: ISO alpha-2 (stored uppercase)
            snapshot_id: ``YYYY_MM_DD`` partition key
            node_to_community: optional {osm_id → community_id} from
                ``CommunityDetectionService.detect_communities``
            signal: optional class signal vector (N,) aligned with
                ``node_order`` — used for per-node Dirichlet contributions
                ``s_i·(Ls)_i`` on the normalized Laplacian
            subgraph_slug: optional subgraph identifier for subdivision-
                scoped spectral analysis.  None for country-level (small
                territories).  When set, rows are scoped to
                (snapshot_id, country_code, subgraph_slug, osm_id) and
                the idempotency delete only touches rows for this subgraph.
            core_ids: optional set of OSM IDs — when provided (Option A
                from the functional maps plan), only entities in
                ``core_ids`` get factor rows.  Buffer-only entities are
                skipped.  This is used when the spectral solve ran on the
                full buffered graph but factor rows should only cover
                core entities.

        Returns:
            Number of rows written.
        """
        from semantic_search.services.knn_graph_service import SparseGraph

        sparse = isinstance(G, SparseGraph)
        if not sparse:
            if G.is_directed():
                G = G.to_undirected()

        node_order = features.get("node_order")
        if not node_order:
            node_order = (
                [int(o) for o in G.node_ids] if sparse else list(G.nodes())
            )
        eigenvectors = np.asarray(features.get("eigenvectors"))
        n = len(node_order)
        if n == 0:
            return 0

        k_actual = eigenvectors.shape[1] if eigenvectors.ndim == 2 else 0

        # Per-node Dirichlet contribution: s_i · (Ls)_i  [GRAPH_REP:Ch3]
        # Uses the combinatorial Laplacian to match
        # GraphSignalService.signal_smoothness (Σᵢ sᵢ·(Ls)ᵢ = sᵀLs).
        dirichlet = None
        if signal is not None and len(signal) == n:
            if sparse:
                L = G.laplacian()
            else:
                L = nx.laplacian_matrix(G, nodelist=node_order).astype(float)
            Ls = np.asarray(L @ signal).ravel()
            dirichlet = np.asarray(signal) * Ls

        if sparse:
            degrees = G.degrees
            degree_map = {
                int(G.node_ids[i]): int(degrees[i]) for i in range(G.n_nodes)
            }
            component_of, component_size_of = G.components()
            if G.n_nodes < self.CLUSTERING_NODE_THRESHOLD:
                clustering = nx.clustering(G.to_nx())
            else:
                logger.info(
                    "FactorNodeWriter: skipping clustering coefficients for "
                    "%d-node graph (≥ %d — stored as NULL)",
                    G.n_nodes, self.CLUSTERING_NODE_THRESHOLD,
                )
                clustering = {}
        else:
            degree_map = dict(G.degree())
            clustering = nx.clustering(G)
            component_of, component_size_of = self._components(G)

        country_code = country_code.upper()

        # Idempotency: replace rows for this (snapshot, country, subgraph)
        del_filter = dict(
            snapshot_id=snapshot_id, country_code=country_code,
        )
        if subgraph_slug is not None:
            del_filter["subgraph_slug"] = subgraph_slug
        else:
            del_filter["subgraph_slug__isnull"] = True
        SpectralNodeMetric.objects.using("vectors").filter(**del_filter).delete()

        rows = []
        for i, osm_id in enumerate(node_order):
            # Option A: skip buffer-only entities when core_ids is provided
            if core_ids is not None and int(osm_id) not in core_ids:
                continue
            if k_actual:
                loadings = np.zeros(EIGEN_LOADING_DIM)
                take = min(k_actual, EIGEN_LOADING_DIM)
                loadings[:take] = eigenvectors[i, :take]
                fiedler = float(eigenvectors[i, 0])
            else:
                loadings = None
                fiedler = None

            rows.append(SpectralNodeMetric(
                snapshot_id=snapshot_id,
                country_code=country_code,
                subgraph_slug=subgraph_slug,
                osm_id=int(osm_id),
                eigen_loadings=loadings.tolist() if loadings is not None else None,
                fiedler_component=fiedler,
                louvain_community=(
                    node_to_community.get(osm_id) if node_to_community else None
                ),
                dirichlet_contrib=(
                    float(dirichlet[i]) if dirichlet is not None else None
                ),
                degree=int(degree_map.get(osm_id, 0)),
                clustering_coeff=(
                    float(clustering[osm_id]) if osm_id in clustering else None
                ),
                component_id=component_of.get(osm_id),
                component_size=component_size_of.get(osm_id),
            ))

        SpectralNodeMetric.objects.using("vectors").bulk_create(
            rows, batch_size=BULK_BATCH_SIZE,
        )
        scope = subgraph_slug or "country"
        logger.info(
            "FactorNodeWriter: wrote %d SpectralNodeMetric rows for %s/%s/%s "
            "(K=%d eigen-loadings)",
            len(rows), country_code, snapshot_id, scope, k_actual,
        )
        return len(rows)

    # ── Step 5d: per-node drift across a snapshot pair ──────────────────

    def write_drift_nodes(
        self,
        country_code: str,
        snapshot_from_id: str,
        snapshot_to_id: str,
    ) -> int:
        """Write DriftNodeMetric rows for a snapshot pair.

        Loads both snapshots' SpectralNodeMetric rows (written by Step 5c),
        sign-aligns the newer eigenbasis against the older one over shared
        nodes (eigsh eigenvector signs are indeterminate per run), then
        computes per-node deltas.

        Only nodes present in BOTH snapshots get rows.

        Returns:
            Number of rows written.
        """
        country_code = country_code.upper()

        def _load(snapshot_id):
            qs = (
                SpectralNodeMetric.objects.using("vectors")
                .filter(snapshot_id=snapshot_id, country_code=country_code)
                .values("osm_id", "fiedler_component", "louvain_community",
                        "degree", "eigen_loadings")
            )
            return {r["osm_id"]: r for r in qs.iterator()}

        rows_from = _load(snapshot_from_id)
        rows_to = _load(snapshot_to_id)
        if not rows_from or not rows_to:
            logger.info(
                "FactorNodeWriter: drift skipped for %s %s→%s "
                "(from=%d rows, to=%d rows)",
                country_code, snapshot_from_id, snapshot_to_id,
                len(rows_from), len(rows_to),
            )
            return 0

        shared = sorted(set(rows_from) & set(rows_to))
        if not shared:
            return 0

        # Build aligned loading matrices over shared nodes, truncated to the
        # smaller K of the pair (large countries compute K=64, padded to 128
        # — trailing zeros contribute nothing to dot products).
        def _matrix(rows):
            mats = [
                np.asarray(rows[o]["eigen_loadings"], dtype=float)
                if rows[o]["eigen_loadings"] is not None
                else np.zeros(EIGEN_LOADING_DIM)
                for o in shared
            ]
            return np.stack(mats)  # (N_shared, 128)

        phi_from = _matrix(rows_from)
        phi_to = _matrix(rows_to)
        k_eff = self._effective_k(phi_from, phi_to)
        phi_from = phi_from[:, :k_eff]
        phi_to = phi_to[:, :k_eff]

        # Sign alignment: flip component k of the "to" basis when it is
        # anti-correlated with the "from" basis over shared nodes.
        # [COHEN:Ch13] — eigenvector sign indeterminacy
        if k_eff:
            dots = np.sum(phi_from * phi_to, axis=0)  # per-component dot
            signs = np.where(dots < 0, -1.0, 1.0)
            phi_to_aligned = phi_to * signs
        else:
            phi_to_aligned = phi_to

        # Idempotency: replace rows for this pair
        DriftNodeMetric.objects.using("vectors").filter(
            country_code=country_code,
            snapshot_from_id=snapshot_from_id,
            snapshot_to_id=snapshot_to_id,
        ).delete()

        drift_rows = []
        for j, osm_id in enumerate(shared):
            rf = rows_from[osm_id]
            rt = rows_to[osm_id]

            fiedler_delta = None
            if k_eff and rf["fiedler_component"] is not None:
                fiedler_delta = float(
                    phi_to_aligned[j, 0] - phi_from[j, 0]
                )

            loading_drift = None
            if k_eff:
                a = phi_from[j]
                b = phi_to_aligned[j]
                denom = np.linalg.norm(a) * np.linalg.norm(b)
                if denom > 0:
                    loading_drift = float(1.0 - np.dot(a, b) / denom)

            degree_delta = None
            if rf["degree"] is not None and rt["degree"] is not None:
                degree_delta = int(rt["degree"] - rf["degree"])

            community_changed = None
            if (rf["louvain_community"] is not None
                    and rt["louvain_community"] is not None):
                community_changed = (
                    rf["louvain_community"] != rt["louvain_community"]
                )

            drift_rows.append(DriftNodeMetric(
                country_code=country_code,
                snapshot_from_id=snapshot_from_id,
                snapshot_to_id=snapshot_to_id,
                osm_id=int(osm_id),
                fiedler_delta=fiedler_delta,
                loading_drift=loading_drift,
                community_changed=community_changed,
                degree_delta=degree_delta,
            ))

        DriftNodeMetric.objects.using("vectors").bulk_create(
            drift_rows, batch_size=BULK_BATCH_SIZE,
        )
        logger.info(
            "FactorNodeWriter: wrote %d DriftNodeMetric rows for %s %s→%s",
            len(drift_rows), country_code, snapshot_from_id, snapshot_to_id,
        )
        return len(drift_rows)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _components(G: nx.Graph):
        """Return ({node → component_id}, {node → component_size})."""
        component_of = {}
        component_size_of = {}
        for cid, comp in enumerate(nx.connected_components(G)):
            size = len(comp)
            for node in comp:
                component_of[node] = cid
                component_size_of[node] = size
        return component_of, component_size_of

    @staticmethod
    def _effective_k(phi_from: np.ndarray, phi_to: np.ndarray) -> int:
        """Number of non-padding components shared by both bases.

        Rows are zero-padded to EIGEN_LOADING_DIM; the effective K is the
        smallest index at which *every* row is zero in either matrix
        (i.e. min(K_from, K_to)).
        """
        def _k(phi):
            if phi.size == 0:
                return 0
            nonzero_cols = np.any(phi != 0.0, axis=0)
            if not nonzero_cols.any():
                return 0
            # K = one past the last column that is non-zero for any node
            return int(np.max(np.nonzero(nonzero_cols))) + 1
        return min(_k(phi_from), _k(phi_to))
