"""FactorResolutionService — runtime (SQL-only) factor-node resolution.

Implements the runtime half of docs/plans/FACTOR_NODE_RUNTIME_JOINS_PLAN.md:
the MapQA executor's SUPPORT/factor nodes resolve to indexed joins against
the ``factor_*`` tables (written by Steps 5c/5d) instead of GraphML loading,
NetworkX traversal, or scipy heat kernels at request time.

In Spatial-Agent terms: this is Algorithm 1's ``ωi(inputs; θi)`` where the
supplementary parameters θi (factor nodes) come from Postgres rows, plus a
mechanical G4 data-availability check (``SELECT EXISTS`` per factor edge).

The centerpiece is ``diffusion_rank``: heat-kernel diffusion from an anchor
node decomposes over the eigenbasis as

    score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)

With per-node eigen-loadings stored in a pgvector column and eigenvalues on
the GraphSpectralFingerprint row, the whole diffusion is ONE query:
``ORDER BY (eigen_loadings <#> :w) ASC`` (pgvector <#> is *negative* inner
product, so ascending = descending score).

References:
- [COHEN:Ch13/Ch15] — Eigendecomposition, matrix exponential
- [GRAPH_REP:Ch3] — Graph Laplacian spectral features
- [SPATIAL_AGENT:§3.3, App F] — factorization, execution semantics
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from worldkg_nca.models import (
    EIGEN_LOADING_DIM,
    AmenityEmbedding,
    SpectralNodeMetric,
)
from semantic_search.services.subgraph_transport_service import (
    SubgraphTransportService,
)

logger = logging.getLogger(__name__)


class FactorResolutionService:
    """Stateless resolver for materialized factor nodes (vectors DB)."""

    # ── G4 data availability ─────────────────────────────────────────────

    def check_availability(
        self, osm_ids, snapshot_id: str, country_code: str,
    ) -> dict:
        """G4 check: does a factor row exist for each osm_id?

        Returns {osm_id: bool}.  A factor edge is executable iff the row
        exists for the current snapshot — this makes the paper's G4
        (data availability) constraint a mechanical SELECT rather than an
        assumption.
        """
        if not osm_ids:
            return {}
        present = set(
            SpectralNodeMetric.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code.upper(),
                osm_id__in=list(osm_ids),
            )
            .values_list("osm_id", flat=True)
        )
        return {int(o): (o in present) for o in osm_ids}

    # ── Metric joins ─────────────────────────────────────────────────────

    def resolve_metrics(
        self, osm_ids, snapshot_id: str, country_code: str,
        subgraph_slug: str = None,
    ) -> dict:
        """Single join: fetch factor rows for a set of osm_ids.

        Returns {osm_id: {fiedler_component, louvain_community, degree,
        clustering_coeff, component_id, component_size, dirichlet_contrib,
        subgraph_slug}}.

        When ``subgraph_slug`` is provided, filters to that subgraph only.
        """
        if not osm_ids:
            return {}
        qs = (
            SpectralNodeMetric.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code.upper(),
                osm_id__in=list(osm_ids),
            )
        )
        if subgraph_slug is not None:
            qs = qs.filter(subgraph_slug=subgraph_slug)
        qs = qs.values(
            "osm_id", "fiedler_component", "louvain_community",
            "degree", "clustering_coeff", "component_id",
            "component_size", "dirichlet_contrib", "subgraph_slug",
        )
        return {int(r["osm_id"]): r for r in qs}

    # ── Heat-kernel diffusion as a pgvector query ────────────────────────

    def diffusion_rank(
        self,
        anchor_osm_id: int,
        t: float,
        snapshot_id: str,
        country_code: str,
        candidate_osm_ids=None,
        limit: int = 20,
        min_score: float = 1e-6,
        trace: list = None,
        cross_subgraph: bool = True,
    ):
        """Rank nodes by heat-kernel diffusion score from an anchor.

        score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)

        Implemented as one pgvector inner-product query against
        ``factor_spectral_node_metric``.  Returns a list of
        {"osm_id", "score"} dicts ordered by descending score, or None
        when the factor tables can't answer (caller falls back to the
        graph/PostGIS path).

        **Subgraph scoping**: if the anchor's factor row has a
        ``subgraph_slug``, the diffusion search and eigenvalue lookup
        are scoped to that subgraph.  Cross-subgraph eigen-loadings are
        NOT directly comparable (different eigenbases), so by default
        the query stays within the anchor's subgraph.

        **Cross-subgraph transport** (``cross_subgraph=True``): when the
        candidate set spans multiple subgraphs and transport matrices
        exist in ``factor_subgraph_transport``, the anchor's heat-kernel
        coefficients are transported to each target subgraph's eigenbasis
        via the functional map C, and a pgvector query runs in each
        target subgraph.  Results are merged and re-ranked by score.
        See ``docs/plans/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md``.
        """
        country_code = country_code.upper()

        # ── Look up anchor + its subgraph_slug ──
        anchor = (
            SpectralNodeMetric.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code,
                osm_id=anchor_osm_id,
                eigen_loadings__isnull=False,
            )
            .values("eigen_loadings", "subgraph_slug")
            .first()
        )
        if anchor is None:
            if trace is not None:
                trace.append({
                    "step": "factor_join",
                    "table": "factor_spectral_node_metric",
                    "warning": f"no factor row for anchor {anchor_osm_id}",
                })
            return None

        subgraph_slug = anchor.get("subgraph_slug")

        # ── Get eigenvalues from the right fingerprint ──
        # Subgraph-scoped: region=subgraph_slug
        # Country-level: region=country_code
        eigenvalues = self._get_eigenvalues(
            snapshot_id, country_code, subgraph_slug=subgraph_slug,
        )
        if not eigenvalues:
            if trace is not None:
                trace.append({
                    "step": "factor_join",
                    "table": "factor_spectral_node_metric",
                    "warning": "no eigenvalues on GraphSpectralFingerprint",
                })
            return None

        # Coefficient vector w_k = e^{-t·λ_k} · φ_k(anchor), zero-padded to
        # EIGEN_LOADING_DIM (padding matches the stored column padding).
        phi_anchor = np.asarray(anchor["eigen_loadings"], dtype=float)
        k = min(len(eigenvalues), EIGEN_LOADING_DIM)
        lam = np.asarray(eigenvalues[:k], dtype=float)
        w = np.zeros(EIGEN_LOADING_DIM)
        w[:k] = np.exp(-t * lam) * phi_anchor[:k]

        # ── Query the anchor's own subgraph (existing behavior) ──
        results = self._query_subgraph(
            snapshot_id=snapshot_id,
            country_code=country_code,
            subgraph_slug=subgraph_slug,
            w=w,
            candidate_osm_ids=candidate_osm_ids,
            limit=limit,
            min_score=min_score,
            anchor_osm_id=anchor_osm_id,
        )

        # ── Cross-subgraph transport (new) ──
        transport_trace = None
        if (
            cross_subgraph
            and subgraph_slug is not None
            and candidate_osm_ids is not None
            and len(candidate_osm_ids) > 0
        ):
            transport_trace = self._cross_subgraph_transport(
                snapshot_id=snapshot_id,
                country_code=country_code,
                anchor_subgraph=subgraph_slug,
                w_anchor=w,
                k_anchor=k,
                eigenvalues_anchor=eigenvalues,
                candidate_osm_ids=candidate_osm_ids,
                limit=limit,
                min_score=min_score,
                anchor_osm_id=anchor_osm_id,
                t=t,
            )
            if transport_trace:
                results.extend(transport_trace["results"])
                # Re-rank by score and trim to limit
                results.sort(key=lambda r: r["score"], reverse=True)
                results = results[:limit]

        if trace is not None:
            trace_entry = {
                "step": "factor_join",
                "table": "factor_spectral_node_metric",
                "op": "heat_kernel_diffusion",
                "anchor_osm_id": anchor_osm_id,
                "subgraph_slug": subgraph_slug,
                "t": t,
                "k_eigenvalues": k,
                "candidates": (
                    len(candidate_osm_ids) if candidate_osm_ids is not None
                    else "all"
                ),
                "output_count": len(results),
            }
            if transport_trace:
                trace_entry["cross_subgraph_transport"] = transport_trace["trace"]
            trace.append(trace_entry)
        return results

    def _query_subgraph(
        self,
        snapshot_id: str,
        country_code: str,
        subgraph_slug: str,
        w: np.ndarray,
        candidate_osm_ids=None,
        limit: int = 20,
        min_score: float = 1e-6,
        anchor_osm_id: int = None,
    ) -> list:
        """Run a pgvector <#> query scoped to a single subgraph.

        Args:
            subgraph_slug: subgraph to query (None for country-level)
            w: EIGEN_LOADING_DIM-dim heat-kernel coefficient vector
            candidate_osm_ids: optional candidate filter
            limit: max results
            min_score: minimum score threshold
            anchor_osm_id: anchor to exclude from results

        Returns:
            List of {"osm_id", "score"} dicts, descending by score.
        """
        qs = SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            country_code=country_code,
            eigen_loadings__isnull=False,
        )
        if subgraph_slug is not None:
            qs = qs.filter(subgraph_slug=subgraph_slug)
        else:
            qs = qs.filter(subgraph_slug__isnull=True)

        if candidate_osm_ids is not None:
            qs = qs.filter(osm_id__in=list(candidate_osm_ids))

        qs = qs.annotate(
            neg_ip=RawSQL(
                "(eigen_loadings <#> %s::vector)",
                (w.tolist(),),
                output_field=FloatField(),
            )
        ).order_by("neg_ip")[: limit + 1]

        results = []
        for row in qs:
            score = -float(row.neg_ip)
            if anchor_osm_id is not None and row.osm_id == anchor_osm_id:
                continue
            if score <= min_score:
                continue
            results.append({"osm_id": int(row.osm_id), "score": score})
            if len(results) >= limit:
                break
        return results

    def _cross_subgraph_transport(
        self,
        snapshot_id: str,
        country_code: str,
        anchor_subgraph: str,
        w_anchor: np.ndarray,
        k_anchor: int,
        eigenvalues_anchor: list,
        candidate_osm_ids: list,
        limit: int,
        min_score: float,
        anchor_osm_id: int,
        t: float,
    ) -> Optional[dict]:
        """Transport anchor's loadings to adjacent subgraphs and query each.

        For each target subgraph that has a transport matrix from the
        anchor's subgraph, transport the heat-kernel coefficients and
        run a pgvector query in the target subgraph's factor rows.

        Returns dict with "results" (list) and "trace" (dict), or None
        if no transport matrices exist or no candidates are in other
        subgraphs.
        """
        # Find which subgraphs the candidates are in (excluding anchor's)
        candidate_subgraphs = set(
            SpectralNodeMetric.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code,
                osm_id__in=list(candidate_osm_ids),
                subgraph_slug__isnull=False,
            )
            .exclude(subgraph_slug=anchor_subgraph)
            .values_list("subgraph_slug", flat=True)
            .distinct()
        )
        if not candidate_subgraphs:
            return None

        all_results = []
        transport_used = []

        for target_slug in sorted(candidate_subgraphs):
            # Load the transport matrix anchor→target
            C = SubgraphTransportService.load_transport_matrix(
                snapshot_id=snapshot_id,
                country_code=country_code,
                subgraph_from=anchor_subgraph,
                subgraph_to=target_slug,
            )
            if C is None:
                continue

            # Get target subgraph's eigenvalues
            eigenvalues_target = self._get_eigenvalues(
                snapshot_id, country_code, subgraph_slug=target_slug,
            )
            if not eigenvalues_target:
                continue

            # Transport the heat-kernel coefficients to the target basis
            # w_anchor is (EIGEN_LOADING_DIM,) — transport the first k components
            k_target = min(len(eigenvalues_target), EIGEN_LOADING_DIM, C.shape[0])
            w_transport = SubgraphTransportService.transport_loadings(
                w_anchor[:k_anchor], C,
            )

            # Re-apply heat kernel decay with target's eigenvalues
            lam_target = np.asarray(eigenvalues_target[:k_target], dtype=float)
            # The transport moved the eigen-loading coefficients; now apply
            # the heat kernel decay in the target's eigenvalue space.
            # w_target[k] = e^{-t·λ_k_target} · w_transport[k]
            w_target = np.zeros(EIGEN_LOADING_DIM)
            w_target[:k_target] = np.exp(-t * lam_target) * w_transport[:k_target]

            # Query the target subgraph
            target_results = self._query_subgraph(
                snapshot_id=snapshot_id,
                country_code=country_code,
                subgraph_slug=target_slug,
                w=w_target,
                candidate_osm_ids=candidate_osm_ids,
                limit=limit,
                min_score=min_score,
                anchor_osm_id=anchor_osm_id,
            )

            all_results.extend(target_results)
            transport_used.append({
                "target_subgraph": target_slug,
                "candidates_found": len(target_results),
            })

        if not transport_used:
            return None

        return {
            "results": all_results,
            "trace": {
                "anchor_subgraph": anchor_subgraph,
                "target_subgraphs": [t["target_subgraph"] for t in transport_used],
                "transport_matrices_used": len(transport_used),
                "candidates_per_subgraph": {
                    t["target_subgraph"]: t["candidates_found"]
                    for t in transport_used
                },
            },
        }

    # ── Community queries ────────────────────────────────────────────────

    def community_summary(
        self,
        snapshot_id: str,
        country_code: str,
        wkg_class: str = None,
        subgraph_slug: str = None,
        trace: list = None,
    ):
        """Community sizes from stored Louvain assignments.

        Returns {"community_count", "communities": [{community_id,
        node_count}, ...]} or None when no rows exist.  ``wkg_class``
        restricts to communities' members by class via an OsmEntity
        subquery join.  ``subgraph_slug`` scopes to a single subgraph.
        """
        from django.db.models import Count

        qs = SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            country_code=country_code.upper(),
            louvain_community__isnull=False,
        )
        if subgraph_slug is not None:
            qs = qs.filter(subgraph_slug=subgraph_slug)
        if wkg_class:
            from worldkg_nca.models import OsmEntity
            member_ids = (
                OsmEntity.objects.using("vectors")
                .filter(
                    snapshot_id=snapshot_id,
                    country_code=country_code.upper(),
                    wkg_class=wkg_class,
                )
                .values("osm_id")
            )
            qs = qs.filter(osm_id__in=member_ids)

        rows = list(
            qs.values("louvain_community")
            .annotate(node_count=Count("osm_id"))
            .order_by("-node_count")
        )
        if not rows:
            return None

        if trace is not None:
            trace.append({
                "step": "factor_join",
                "table": "factor_spectral_node_metric",
                "op": "community_summary",
                "community_count": len(rows),
                "wkg_class": wkg_class,
            })
        return {
            "community_count": len(rows),
            "communities": [
                {
                    "community_id": r["louvain_community"],
                    "node_count": r["node_count"],
                }
                for r in rows
            ],
        }

    # ── Amenity embeddings (replaces runtime FastText) ───────────────────

    def amenity_embedding(self, amenity_text: str):
        """Look up the precomputed FastText embedding for an amenity string.

        Returns a 300D numpy array, or None when the text isn't in the
        vocabulary (caller falls back to runtime FastText — the parser's
        OBJECT extraction is open-vocabulary).
        """
        row = (
            AmenityEmbedding.objects.using("vectors")
            .filter(amenity_text=amenity_text.strip().lower())
            .values("embedding")
            .first()
        )
        if row is None:
            return None
        return np.asarray(row["embedding"], dtype=float)

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _get_eigenvalues(
        snapshot_id: str, country_code: str, subgraph_slug: str = None,
    ):
        """Eigenvalues λ₁..λ_K from the GraphSpectralFingerprint (default DB).

        When ``subgraph_slug`` is set, looks up the subgraph-scoped
        fingerprint (region=subgraph_slug).  Otherwise falls back to the
        country-level fingerprint (region=country_code).

        Returns a list of floats, or None when no fingerprint exists.
        """
        from osmsnapshot.models import Snapshot
        from semantic_search.models import GraphSpectralFingerprint

        snapshot = (
            Snapshot.objects.using("default")
            .filter(country_code__iexact=country_code, snapshot_date=snapshot_id)
            .order_by("-created_at")
            .first()
        )
        if snapshot is None:
            return None

        # Try subgraph-scoped fingerprint first
        if subgraph_slug is not None:
            fp = (
                GraphSpectralFingerprint.objects
                .filter(region__iexact=subgraph_slug, snapshot=snapshot)
                .order_by("-created_at")
                .first()
            )
            if fp is not None:
                return fp.eigenvalues

        # Fall back to country-level fingerprint
        fp = (
            GraphSpectralFingerprint.objects
            .filter(region__iexact=country_code, snapshot=snapshot)
            .order_by("-created_at")
            .first()
        )
        if fp is None:
            return None
        return fp.eigenvalues
