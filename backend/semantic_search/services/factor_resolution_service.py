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

import numpy as np
from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from worldkg_nca.models import (
    EIGEN_LOADING_DIM,
    AmenityEmbedding,
    SpectralNodeMetric,
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
    ) -> dict:
        """Single join: fetch factor rows for a set of osm_ids.

        Returns {osm_id: {fiedler_component, louvain_community, degree,
        clustering_coeff, component_id, component_size, dirichlet_contrib}}.
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
            .values(
                "osm_id", "fiedler_component", "louvain_community",
                "degree", "clustering_coeff", "component_id",
                "component_size", "dirichlet_contrib",
            )
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
    ):
        """Rank nodes by heat-kernel diffusion score from an anchor.

        score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)

        Implemented as one pgvector inner-product query against
        ``factor_spectral_node_metric``.  Returns a list of
        {"osm_id", "score"} dicts ordered by descending score, or None
        when the factor tables can't answer (caller falls back to the
        graph/PostGIS path).
        """
        country_code = country_code.upper()

        anchor = (
            SpectralNodeMetric.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code,
                osm_id=anchor_osm_id,
                eigen_loadings__isnull=False,
            )
            .values("eigen_loadings")
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

        eigenvalues = self._get_eigenvalues(snapshot_id, country_code)
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

        qs = SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            country_code=country_code,
            eigen_loadings__isnull=False,
        )
        if candidate_osm_ids is not None:
            qs = qs.filter(osm_id__in=list(candidate_osm_ids))

        qs = qs.annotate(
            neg_ip=RawSQL(
                "(eigen_loadings <#> %s::vector)",
                (w.tolist(),),
                output_field=FloatField(),
            )
        ).order_by("neg_ip")[: limit + 1]  # +1: anchor may outrank itself

        results = []
        for row in qs:
            score = -float(row.neg_ip)  # <#> returns negative inner product
            if row.osm_id == anchor_osm_id:
                continue
            if score <= min_score:
                continue
            results.append({"osm_id": int(row.osm_id), "score": score})
            if len(results) >= limit:
                break

        if trace is not None:
            trace.append({
                "step": "factor_join",
                "table": "factor_spectral_node_metric",
                "op": "heat_kernel_diffusion",
                "anchor_osm_id": anchor_osm_id,
                "t": t,
                "k_eigenvalues": k,
                "candidates": (
                    len(candidate_osm_ids) if candidate_osm_ids is not None
                    else "all"
                ),
                "output_count": len(results),
            })
        return results

    # ── Community queries ────────────────────────────────────────────────

    def community_summary(
        self,
        snapshot_id: str,
        country_code: str,
        wkg_class: str = None,
        trace: list = None,
    ):
        """Community sizes from stored Louvain assignments.

        Returns {"community_count", "communities": [{community_id,
        node_count}, ...]} or None when no rows exist.  ``wkg_class``
        restricts to communities' members by class via an OsmEntity
        subquery join.
        """
        from django.db.models import Count

        qs = SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            country_code=country_code.upper(),
            louvain_community__isnull=False,
        )
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
    def _get_eigenvalues(snapshot_id: str, country_code: str):
        """Eigenvalues λ₁..λ_K from the GraphSpectralFingerprint (default DB).

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
        fp = (
            GraphSpectralFingerprint.objects
            .filter(region__iexact=country_code, snapshot=snapshot)
            .order_by("-created_at")
            .first()
        )
        if fp is None:
            return None
        return fp.eigenvalues
