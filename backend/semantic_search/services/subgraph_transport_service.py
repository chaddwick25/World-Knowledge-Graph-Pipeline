"""SubgraphTransportService — functional map computation and runtime transport.

Implements the batch-time computation and runtime application of k×k functional
map matrices between adjacent subgraph eigenbases, per
``docs/plans/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md``.

Grounded in:
- Ovsjanikov et al. 2012 — ``C = Φ_Bᵀ S Φ_A`` (functional map definition)
- Pegoraro et al. 2023 — spectral maps for graphs/subgraphs (Eq. 1, 2)
- GRASP (Charneau et al. 2022) — regularized least squares for C
- Behmanesh et al. ICML 2026 — Laplacian commutativity regularizer

Batch time (Step 5c Phase B):
    For each adjacent subgraph pair (A, B):
      1. Identify shared entities (in both buffered graphs — Option A)
      2. Extract their eigen-loadings in both bases: F_A (m×k), F_B (m×k)
      3. Solve regularized least squares:
         C = argmin ‖F_B - F_A Cᵀ‖² + λ‖CΛ_A - Λ_B C‖²
      4. Store C in ``factor_subgraph_transport``

Runtime (FactorResolutionService.diffusion_rank):
    loadings_b = C_{A→B} · loadings_a   (one k×k matrix-vector multiply)
    Then the existing pgvector <#> query runs in B's factor rows.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

from worldkg_nca.models import SubgraphTransport

logger = logging.getLogger(__name__)

# Minimum number of shared entities required to fit a transport matrix.
# Below this, the least-squares problem is under-determined.
MIN_SHARED_ENTITIES = 16


class SubgraphTransportService:
    """Compute and apply functional map matrices between subgraph eigenbases.

    Stateless — all methods are pure functions of their arguments (except
    ``write_transport_matrices`` and ``load_transport_matrix``, which hit
    the vectors DB).
    """

    # ── Batch-time: compute C ────────────────────────────────────────────

    def compute_transport_matrix(
        self,
        eigenvalues_a: np.ndarray,
        eigenvectors_a: np.ndarray,
        node_ids_a: np.ndarray,
        eigenvalues_b: np.ndarray,
        eigenvectors_b: np.ndarray,
        node_ids_b: np.ndarray,
        shared_node_ids: np.ndarray,
        lambda_reg: float = 1e-3,
    ) -> Optional[dict]:
        """Compute the k×k functional map C_{A→B}.

        Args:
            eigenvalues_a: (k,) subgraph A eigenvalues (λ₁..λ_k, sorted)
            eigenvectors_a: (n_a, k) subgraph A eigenvectors (Φ_A)
            node_ids_a: (n_a,) subgraph A node IDs (OSM IDs)
            eigenvalues_b: (k,) subgraph B eigenvalues
            eigenvectors_b: (n_b, k) subgraph B eigenvectors (Φ_B)
            node_ids_b: (n_b,) subgraph B node IDs
            shared_node_ids: (m,) IDs present in both A and B (correspondence)
            lambda_reg: commutativity regularizer weight (default 1e-3)

        Returns:
            dict with ``transport_matrix`` (k×k np.ndarray), ``k_dim``,
            ``shared_entity_count``, ``fit_residual``,
            ``commutativity_residual``, or None if insufficient shared
            entities.
        """
        k = min(len(eigenvalues_a), len(eigenvalues_b))
        if k < 2:
            logger.warning("Transport: k=%d too small — skipping", k)
            return None

        m = len(shared_node_ids)
        if m < MIN_SHARED_ENTITIES:
            logger.info(
                "Transport: only %d shared entities (need ≥ %d) — skipping",
                m, MIN_SHARED_ENTITIES,
            )
            return None

        # Build index maps: osm_id → row in eigenvector matrix
        idx_a = {int(oid): i for i, oid in enumerate(node_ids_a)}
        idx_b = {int(oid): i for i, oid in enumerate(node_ids_b)}

        # Extract eigen-loadings for shared entities in both bases
        # F_A: (m, k) — shared entities' loadings in A's basis
        # F_B: (m, k) — shared entities' loadings in B's basis
        rows_a, rows_b = [], []
        for oid in shared_node_ids:
            oid = int(oid)
            if oid in idx_a and oid in idx_b:
                rows_a.append(eigenvectors_a[idx_a[oid], :k])
                rows_b.append(eigenvectors_b[idx_b[oid], :k])

        m_eff = len(rows_a)
        if m_eff < MIN_SHARED_ENTITIES:
            logger.info(
                "Transport: only %d shared entities found in both graphs "
                "(need ≥ %d) — skipping",
                m_eff, MIN_SHARED_ENTITIES,
            )
            return None

        F_A = np.stack(rows_a)  # (m, k)
        F_B = np.stack(rows_b)  # (m, k)

        # Truncate eigenvalues to k
        lam_a = np.asarray(eigenvalues_a[:k], dtype=float)
        lam_b = np.asarray(eigenvalues_b[:k], dtype=float)

        # Solve regularized least squares:
        #   C = argmin ‖F_B - F_A Cᵀ‖² + λ‖CΛ_A - Λ_B C‖²
        #
        # The descriptor term: minimize ‖F_B - F_A Cᵀ‖²
        #   → Cᵀ = (F_Aᵀ F_A)⁻¹ F_Aᵀ F_B  (ordinary least squares)
        #   → C = F_Bᵀ F_A (F_Aᵀ F_A)⁻¹
        #
        # With the commutativity regularizer, the full objective is:
        #   min_C ‖F_B - F_A Cᵀ‖_F² + λ ‖C Λ_A - Λ_B C‖_F²
        #
        # This is a convex quadratic in C. We solve it by vectorizing:
        #   vec(C) = argmin ‖vec(F_B) - (F_A ⊗ I_k) vec(Cᵀ)‖²
        #           + λ ‖(Λ_A ⊗ I_k - I_k ⊗ Λ_B) vec(C)‖²
        #
        # For simplicity and numerical stability, we use scipy's least
        # squares with the regularizer appended as additional rows.

        C = self._solve_regularized_transport(
            F_A, F_B, lam_a, lam_b, lambda_reg,
        )

        # Compute residuals
        fit_residual = self._fit_residual(F_A, F_B, C)
        commutativity_residual = self._commutativity_residual(C, lam_a, lam_b)

        logger.info(
            "Transport: C_%dx%d computed — %d shared entities, "
            "fit_residual=%.4f, commutativity_residual=%.4f",
            k, k, m_eff, fit_residual, commutativity_residual,
        )

        return {
            "transport_matrix": C,
            "k_dim": k,
            "shared_entity_count": m_eff,
            "fit_residual": float(fit_residual),
            "commutativity_residual": float(commutativity_residual),
        }

    @staticmethod
    def _solve_regularized_transport(
        F_A: np.ndarray,
        F_B: np.ndarray,
        lam_a: np.ndarray,
        lam_b: np.ndarray,
        lambda_reg: float,
    ) -> np.ndarray:
        """Solve the regularized least-squares problem for C.

        min_C ‖F_B - F_A Cᵀ‖_F² + λ ‖C Λ_A - Λ_B C‖_F²

        We solve for C directly (not Cᵀ) by setting up the normal equations
        with the commutativity regularizer appended as Tikhonov rows.

        The descriptor term gives:
            F_A Cᵀ ≈ F_B   →   C F_Aᵀ ≈ F_Bᵀ
            vec(C) via: (F_A ⊗ I) vec(Cᵀ) = vec(F_B)

        We reformulate: let x = vec(C) (column-major, k²×1).
        Descriptor:  ‖F_B - F_A Cᵀ‖² = ‖vec(F_B) - (I_k ⊗ F_A) vec(Cᵀ)‖²
                     But vec(Cᵀ) = P vec(C) where P is the perfect shuffle
                     permutation. To avoid this complexity, we solve for
                     Cᵀ directly and transpose.

        Simpler approach: solve for Cᵀ via ordinary least squares with
        commutativity regularizer on C (not Cᵀ):

            min_{Cᵀ} ‖F_B - F_A Cᵀ‖² + λ ‖C Λ_A - Λ_B C‖²

        The commutativity term ‖C Λ_A - Λ_B C‖_F² can be written as:
            ‖C Λ_A - Λ_B C‖_F² = ‖(Λ_A ⊗ I_k) vec(C) - (I_k ⊗ Λ_B) vec(C)‖²
                               = ‖(Λ_A ⊗ I_k - I_k ⊗ Λ_B) vec(C)‖²

        Since vec(C) = P vec(Cᵀ) for permutation P, and we're solving for
        Cᵀ, we apply the regularizer to C = (Cᵀ)ᵀ.

        For numerical simplicity, we solve the unregularized OLS first,
        then apply the commutativity regularizer as a post-hoc Tikhonov
        ridge. This is equivalent to the closed-form solution of the
        regularized problem when the regularizer is quadratic.
        """
        k = F_A.shape[1]

        # Descriptor term: F_A Cᵀ ≈ F_B  →  Cᵀ = lstsq(F_A, F_B)
        # This gives the ordinary least squares solution.
        C_T, _, _, _ = np.linalg.lstsq(F_A, F_B, rcond=None)
        C = C_T.T  # (k, k)

        if lambda_reg <= 0:
            return C

        # Apply commutativity regularizer via iterative refinement.
        # The regularizer ‖C Λ_A - Λ_B C‖² pushes C toward commuting
        # with the Laplacians. We solve:
        #   min_C ‖F_B - F_A Cᵀ‖² + λ ‖C Λ_A - Λ_B C‖²
        #
        # Using the vectorized form:
        #   A_desc = I_k ⊗ F_A          (descriptor, mk × k²)
        #   A_comm = Λ_A ⊗ I_k - I_k ⊗ Λ_B  (commutativity, k² × k²)
        #   b = vec(F_B)                (mk × 1)
        #   x = vec(Cᵀ)                 (k² × 1)
        #
        # Solution: x = (A_descᵀ A_desc + λ A_commᵀ A_comm)⁻¹ A_descᵀ b

        # Build the commutativity matrix: (Λ_A ⊗ I - I ⊗ Λ_B)
        # This is k² × k². For k=128, that's 16384 × 16384 — too large
        # to build densely. Use the sparse structure instead.
        #
        # (Λ_A ⊗ I) vec(C) has entries: lam_a[i] * C[i,j]
        # (I ⊗ Λ_B) vec(C) has entries: lam_b[j] * C[i,j]
        # So (Λ_A ⊗ I - I ⊗ Λ_B) vec(C) has entries: (lam_a[i] - lam_b[j]) * C[i,j]
        #
        # The regularizer ‖comm‖² = Σ_{i,j} (lam_a[i] - lam_b[j])² * C[i,j]²
        #
        # This is a diagonal operator on vec(C)! So the Tikhonov matrix is
        # diagonal with entries (lam_a[i] - lam_b[j])².
        #
        # The full regularized solution is:
        #   C[i,j] = C_ols[i,j] / (1 + λ (lam_a[i] - lam_b[j])² / ‖F_A[:,i]‖²)
        #
        # But this is an approximation. For the exact solution, we solve
        # the k² × k² system. Since the regularizer is diagonal, we can
        # use the Woodbury identity or just solve directly.
        #
        # For simplicity and correctness, we solve the k² × k² system
        # using the diagonal structure of the regularizer.

        # Build the diagonal of the commutativity penalty
        # diff[i,j] = (lam_a[i] - lam_b[j])²
        diff_sq = (lam_a[:, None] - lam_b[None, :]) ** 2  # (k, k)

        # The descriptor normal matrix: F_Aᵀ F_A (k × k)
        FtF = F_A.T @ F_A  # (k, k)
        FtB = F_A.T @ F_B  # (k, k) — this is the RHS for Cᵀ

        # The regularized normal equations for Cᵀ:
        # (FtF + λ * diag(diff_sq)) Cᵀ = FtB
        # where diag(diff_sq) is applied element-wise to the diagonal
        # of the k² × k² system. But since Cᵀ has shape (k, k), and the
        # regularizer is diagonal in the vec(C) representation, we need
        # to be careful about the row/column mapping.
        #
        # vec(Cᵀ) stacks columns of Cᵀ = rows of C.
        # The regularizer entry for C[i,j] is (lam_a[i] - lam_b[j])².
        # In vec(Cᵀ), C[i,j] is at position j*k + i.
        #
        # The descriptor normal matrix for vec(Cᵀ) is I_k ⊗ (F_Aᵀ F_A),
        # which is block-diagonal with k blocks of FtF.
        #
        # The regularizer is diagonal with entries (lam_a[i] - lam_b[j])²
        # at position j*k + i.
        #
        # So the regularized system is:
        # (I_k ⊗ FtF + λ * diag(diff_sq_flat)) vec(Cᵀ) = vec(FtB)
        #
        # where diff_sq_flat[j*k + i] = (lam_a[i] - lam_b[j])²

        # Solve column-by-column of Cᵀ (each column is independent
        # because I_k ⊗ FtF is block-diagonal)
        diff_sq_flat = diff_sq.ravel()  # (k²,) — row-major: [i,j] at i*k+j
        # But we need it in vec(Cᵀ) order (column-major of Cᵀ = row-major of C)
        # vec(Cᵀ)[j*k + i] = Cᵀ[i,j] = C[j,i]
        # diff_sq for C[j,i] = (lam_a[j] - lam_b[i])²
        # So diff_sq_vec[j*k + i] = (lam_a[j] - lam_b[i])²
        # = diff_sq[j, i] = diff_sq_flat[j*k + i]
        # This is just the transpose of diff_sq, flattened row-major.
        diff_sq_vec = diff_sq.T.ravel()  # (k²,)

        # Solve for each column j of Cᵢ (i.e., each row j of C)
        # Column j of Cᵀ = row j of C
        # The system for column j: (FtF + λ * diag(diff_sq[j,:])) c_j = FtB[:,j]
        C_reg = np.zeros((k, k))
        for j in range(k):
            reg_diag = lambda_reg * diff_sq[j, :]  # (k,) — regularizer for row j of C
            A_j = FtF + np.diag(reg_diag)
            b_j = FtB[:, j]
            try:
                c_j = np.linalg.solve(A_j, b_j)
            except np.linalg.LinAlgError:
                c_j = np.linalg.lstsq(A_j, b_j, rcond=None)[0]
            C_reg[j, :] = c_j  # row j of C

        return C_reg

    @staticmethod
    def _fit_residual(F_A: np.ndarray, F_B: np.ndarray, C: np.ndarray) -> float:
        """Compute ‖F_B - F_A Cᵀ‖_F / ‖F_B‖_F."""
        F_B_pred = F_A @ C.T
        numerator = np.linalg.norm(F_B - F_B_pred, 'fro')
        denominator = np.linalg.norm(F_B, 'fro')
        if denominator < 1e-12:
            return 0.0
        return float(numerator / denominator)

    @staticmethod
    def _commutativity_residual(
        C: np.ndarray, lam_a: np.ndarray, lam_b: np.ndarray,
    ) -> float:
        """Compute ‖CΛ_A - Λ_B C‖_F / ‖Λ_A‖_F."""
        # C Λ_A: scale each column j of C by lam_a[j]
        C_LamA = C * lam_a[None, :]
        # Λ_B C: scale each row i of C by lam_b[i]
        LamB_C = C * lam_b[:, None]
        numerator = np.linalg.norm(C_LamA - LamB_C, 'fro')
        denominator = np.linalg.norm(lam_a)
        if denominator < 1e-12:
            return 0.0
        return float(numerator / denominator)

    # ── Runtime: transport loadings ──────────────────────────────────────

    @staticmethod
    def transport_loadings(
        loadings_a: np.ndarray,
        transport_matrix: np.ndarray,
    ) -> np.ndarray:
        """Transport eigen-loadings from A's eigenbasis to B's eigenbasis.

        loadings_b = C_{A→B} · loadings_a

        This is the runtime operation: a single k×k matrix-vector multiply
        (128×128 × 128 = 16K FLOPs), then the existing pgvector <#> query
        runs in B's factor rows.

        Args:
            loadings_a: (k,) or (EIGEN_LOADING_DIM,) eigen-loadings in A's basis
            transport_matrix: (k, k) C_{A→B}

        Returns:
            (k,) transported loadings in B's basis
        """
        C = np.asarray(transport_matrix, dtype=float)
        k = C.shape[0]
        phi = np.asarray(loadings_a, dtype=float)
        # Truncate/pad to k
        if len(phi) > k:
            phi = phi[:k]
        elif len(phi) < k:
            phi = np.pad(phi, (0, k - len(phi)))
        return C @ phi

    # ── Adjacency computation ────────────────────────────────────────────

    def compute_adjacency(
        self,
        subgraph_configs: list,
        buffer_deg: float = 0.45,
    ) -> List[Tuple[str, str]]:
        """Determine which subgraph pairs are adjacent (share a border).

        Two subgraphs are adjacent if their strict polygons intersect or
        their bounding boxes are within 2× buffer_deg of each other.

        Args:
            subgraph_configs: list of SubgraphConfig-like objects with
                ``slug``, ``poly_path``, and ``bbox_*`` attributes
            buffer_deg: buffer distance in degrees (default 0.45° ≈ 50 km)

        Returns:
            List of (slug_a, slug_b) pairs, sorted by slug_a then slug_b.
            Each pair appears once (a < b lexicographically).
        """
        from django.contrib.gis.geos import GEOSGeometry

        pairs = []
        n = len(subgraph_configs)

        # Try polygon-based adjacency first
        polygons = {}
        for sg in subgraph_configs:
            poly = self._load_polygon(sg)
            if poly is not None:
                polygons[sg.slug] = poly

        for i in range(n):
            for j in range(i + 1, n):
                sg_a = subgraph_configs[i]
                sg_b = subgraph_configs[j]
                slug_a, slug_b = sorted([sg_a.slug, sg_b.slug])

                # Polygon-based check
                if slug_a in polygons and slug_b in polygons:
                    pa = polygons[slug_a]
                    pb = polygons[slug_b]
                    # Adjacent if polygons intersect or are within 2× buffer
                    if pa.intersects(pb) or pa.buffer(buffer_deg).intersects(pb.buffer(buffer_deg)):
                        pairs.append((slug_a, slug_b))
                        continue

                # BBox-based fallback
                if self._bboxes_are_adjacent(sg_a, sg_b, buffer_deg):
                    pairs.append((slug_a, slug_b))

        pairs.sort()
        return pairs

    @staticmethod
    def _load_polygon(sg) -> Optional[GEOSGeometry]:
        """Load a subgraph's strict polygon from its .poly file."""
        import os
        poly_path = getattr(sg, 'poly_path', None) or getattr(sg, 'subgraph_poly_path', None)
        if not poly_path or not os.path.exists(poly_path):
            return None
        try:
            from core.services.planet_init.geofabrik_poly_service import parse_poly_file
            wkt = parse_poly_file(poly_path)
            if wkt:
                return GEOSGeometry(wkt, srid=4326)
        except Exception as exc:
            logger.debug("Transport adjacency: failed to parse poly for %s: %s",
                         sg.slug, exc)
        return None

    @staticmethod
    def _bboxes_are_adjacent(sg_a, sg_b, buffer_deg: float) -> bool:
        """Check if two subgraphs' bboxes are within 2× buffer_deg."""
        a_lon = (getattr(sg_a, 'bbox_min_lon', None), getattr(sg_a, 'bbox_max_lon', None))
        a_lat = (getattr(sg_a, 'bbox_min_lat', None), getattr(sg_a, 'bbox_max_lat', None))
        b_lon = (getattr(sg_b, 'bbox_min_lon', None), getattr(sg_b, 'bbox_max_lon', None))
        b_lat = (getattr(sg_b, 'bbox_min_lat', None), getattr(sg_b, 'bbox_max_lat', None))

        if any(v is None for v in (*a_lon, *a_lat, *b_lon, *b_lat)):
            return False

        gap = 2 * buffer_deg
        # Bboxes are adjacent if they overlap or are within gap in both lon and lat
        lon_adj = a_lon[0] <= b_lon[1] + gap and b_lon[0] <= a_lon[1] + gap
        lat_adj = a_lat[0] <= b_lat[1] + gap and b_lat[0] <= a_lat[1] + gap
        return lon_adj and lat_adj

    # ── DB I/O ───────────────────────────────────────────────────────────

    def write_transport_matrices(
        self,
        snapshot_id: str,
        country_code: str,
        transport_data: List[dict],
    ) -> int:
        """Batch-write transport matrices to ``factor_subgraph_transport``.

        Idempotent: deletes existing rows for (snapshot_id, country_code)
        before inserting.

        Args:
            snapshot_id: snapshot date (YYYY_MM_DD)
            country_code: ISO alpha-2 country code
            transport_data: list of dicts with keys:
                subgraph_from, subgraph_to, transport_matrix (np.ndarray),
                k_dim, shared_entity_count, fit_residual,
                commutativity_residual

        Returns:
            Number of rows written.
        """
        country_code = country_code.upper()

        # Idempotent delete
        SubgraphTransport.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            country_code=country_code,
        ).delete()

        if not transport_data:
            return 0

        rows = []
        for td in transport_data:
            C = np.asarray(td["transport_matrix"], dtype=float)
            rows.append(SubgraphTransport(
                snapshot_id=snapshot_id,
                country_code=country_code,
                subgraph_from=td["subgraph_from"],
                subgraph_to=td["subgraph_to"],
                transport_matrix=C.tolist(),
                k_dim=td["k_dim"],
                shared_entity_count=td["shared_entity_count"],
                fit_residual=td.get("fit_residual"),
                commutativity_residual=td.get("commutativity_residual"),
            ))

        SubgraphTransport.objects.using("vectors").bulk_create(
            rows, batch_size=100,
        )
        logger.info(
            "Transport: wrote %d SubgraphTransport rows for %s/%s",
            len(rows), country_code, snapshot_id,
        )
        return len(rows)

    @staticmethod
    def load_transport_matrix(
        snapshot_id: str,
        country_code: str,
        subgraph_from: str,
        subgraph_to: str,
    ) -> Optional[np.ndarray]:
        """Load a transport matrix from the vectors DB.

        Returns the k×k np.ndarray, or None if no transport matrix exists
        for this pair (non-adjacent or insufficient shared entities).
        """
        row = (
            SubgraphTransport.objects.using("vectors")
            .filter(
                snapshot_id=snapshot_id,
                country_code=country_code.upper(),
                subgraph_from=subgraph_from,
                subgraph_to=subgraph_to,
            )
            .values("transport_matrix", "k_dim")
            .first()
        )
        if row is None:
            return None
        return np.asarray(row["transport_matrix"], dtype=float)
