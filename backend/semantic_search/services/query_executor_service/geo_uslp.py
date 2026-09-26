"""GeoUslpMixin — extracted from query_executor_service.py (monolith split, Phase 5)."""

import logging
import math
import re

from semantic_search.services.query_executor_service._constants import (
    USLP_GEOHASH_PRECISION,
    USLP_FALLBACK_D_MAX_KM,
)

logger = logging.getLogger(__name__)


class GeoUslpMixin:
    """Mixin providing executor methods to QueryExecutorService."""

    @classmethod
    def _enrich_with_geo_and_uslp(cls, results, template, anchor_coords,
                                   radius_m, anchor_osm_id, country_code,
                                   snapshot_date, trace):
        """Add geo_score (template-aware) and USLP boost to results, re-rank.

        geo_score uses the USLP geohash-based formula (Mann et al. 2023 §3.3):
        encode anchor and candidate at P4 precision, compute haversine between
        cluster centers, normalize by d_max. For FILTER-AGGREGATE-MEASURE with
        an explicit radius, uses raw haversine with the user's radius as d_max.
        USLP boost adds a small score for entities that appear as predicted
        link tails from the anchor entity.

        Modifies results in-place and re-sorts by combined_score.
        """
        if not results:
            return results

        # Compute geo_score for each result
        anchor_lat = anchor_coords.get("lat") if anchor_coords else None
        anchor_lon = anchor_coords.get("lon") if anchor_coords else None
        has_geo = anchor_lat is not None and anchor_lon is not None

        if has_geo:
            for r in results:
                r_lat = r.get("lat")
                r_lon = r.get("lon")
                if r_lat is not None and r_lon is not None:
                    r["geo_score"] = round(
                        cls._geo_score_uslp(
                            anchor_lat, anchor_lon, r_lat, r_lon,
                            template, radius_m,
                        ), 4
                    )
                    # Also store raw distance for the radius guard
                    r["distance_km"] = round(
                        cls._haversine_km(anchor_lat, anchor_lon, r_lat, r_lon), 3
                    )
                else:
                    r["distance_km"] = None
                    r["geo_score"] = 0.0
        else:
            for r in results:
                r["distance_km"] = None
                r["geo_score"] = 0.0

        # USLP signal boost — entities that appear as predicted link tails
        # from the anchor get a small boost. This connects the link
        # prediction layer to search ranking.
        uslp_tail_ids = set()
        if anchor_osm_id and country_code:
            uslp_tail_ids = cls._get_uslp_predicted_tails(
                anchor_osm_id, country_code, snapshot_date, trace,
            )

        if uslp_tail_ids:
            boosted = 0
            for r in results:
                if r.get("osm_id") in uslp_tail_ids:
                    r["uslp_boost"] = 0.5
                    boosted += 1
                else:
                    r["uslp_boost"] = 0.0
            if trace is not None:
                trace.append({
                    "step": "uslp_boost",
                    "anchor_osm_id": anchor_osm_id,
                    "predicted_tails": len(uslp_tail_ids),
                    "boosted_results": boosted,
                })
        else:
            for r in results:
                r["uslp_boost"] = 0.0

        # Re-rank by combined score: diffusion_score + geo_score + uslp_boost
        for r in results:
            diff = r.get("diffusion_score", 0.0)
            geo = r.get("geo_score", 0.0)
            uslp = r.get("uslp_boost", 0.0)
            r["combined_score"] = round(diff + geo + uslp, 6)

        results.sort(key=lambda r: r.get("combined_score", 0.0), reverse=True)
        return results
    @classmethod
    def _get_uslp_predicted_tails(cls, head_osm_id, country_code,
                                   snapshot_date, trace):
        """Return set of tail osm_ids from accepted USLP links for this head.

        Queries SpatialTripletScore on the vectors DB for predicted links
        (normalized_score >= 0.7) where the head matches. Returns an empty
        set if USLP hasn't been run or no links exist.
        """
        try:
            from igea.models import SpatialTripletScore
            snapshot_id = cls._get_snapshot_id(snapshot_date)
            qs = SpatialTripletScore.objects.using("vectors").filter(
                head_osm_id=head_osm_id,
                predicted=True,
            )
            if snapshot_id:
                qs = qs.filter(snapshot_id=snapshot_id)
            tail_ids = set(qs.values_list("tail_osm_id", flat=True)[:200])
            return tail_ids
        except Exception as e:
            if trace is not None:
                trace.append({
                    "step": "uslp_lookup",
                    "warning": f"USLP lookup failed: {e}",
                })
            return set()

    # ── Geometric helpers ───────────────────────────────────────────────────
    @staticmethod
    def _haversine_km(lat1: float, lon1: float,
                      lat2: float, lon2: float) -> float:
        """Haversine distance in kilometers."""
        R = 6371  # Earth's mean radius in km
        phi1, lam1 = math.radians(lat1), math.radians(lon1)
        phi2, lam2 = math.radians(lat2), math.radians(lon2)
        dphi = phi2 - phi1
        dlam = lam2 - lam1
        h = (math.sin(dphi / 2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
        return 2 * R * math.atan2(math.sqrt(h), math.sqrt(1 - h))
    @classmethod
    def _haversine_m(cls, lat1, lon1, lat2, lon2) -> float:
        return cls._haversine_km(lat1, lon1, lat2, lon2) * 1000
    @classmethod
    def _geo_score_uslp(cls, head_lat: float, head_lon: float,
                        tail_lat: float, tail_lon: float,
                        template: str, radius_m: int = None) -> float:
        """USLP geographic space score (Mann et al. 2023 §3.3).

        Uses geohash cluster centers at the precision level appropriate for
        the template, then normalizes by d_max:
            geo_score = 1 - d_cluster / d_max

        - OBJECT-FIELD-MEASURE: returns 0.0 (distance IS the answer)
        - FILTER-AGGREGATE-MEASURE: uses raw haversine with the user's
          explicit radius as d_max (no geohash quantization)
        - All other templates: P4 geohash (~39km cells), d_max = P4 cell width

        The paper computes d_max from the candidate pool's cluster-center
        distance matrix. At runtime without a precomputed pool, we use the
        geohash cell width as the fallback d_max (see USLP_FALLBACK_D_MAX_KM).
        """
        if template == "OBJECT-FIELD-MEASURE (#2)":
            return 0.0

        # FILTER-AGGREGATE-MEASURE: user specified an exact radius — use it
        if template == "FILTER-AGGREGATE-MEASURE (#1)" and radius_m:
            d_max = radius_m / 1000.0
            dist_km = cls._haversine_km(head_lat, head_lon, tail_lat, tail_lon)
            return max(0.0, min(1.0, 1.0 - (dist_km / d_max)))

        # All other templates: USLP geohash-based scoring at P4
        precision = USLP_GEOHASH_PRECISION
        try:
            import geohash2
            gh_h = geohash2.encode(head_lat, head_lon, precision=precision)
            gh_t = geohash2.encode(tail_lat, tail_lon, precision=precision)
            # Decode to cluster centers
            lat_h_str, lon_h_str = geohash2.decode(gh_h)
            lat_t_str, lon_t_str = geohash2.decode(gh_t)
            c_h = (float(lat_h_str), float(lon_h_str))
            c_t = (float(lat_t_str), float(lon_t_str))
            dist_km = cls._haversine_km(c_h[0], c_h[1], c_t[0], c_t[1])
        except Exception:
            # Fallback to raw haversine if geohash fails
            dist_km = cls._haversine_km(head_lat, head_lon, tail_lat, tail_lon)

        d_max = USLP_FALLBACK_D_MAX_KM.get(precision, 39.0)
        return max(0.0, min(1.0, 1.0 - (dist_km / d_max)))

    # ── Answer synthesis ────────────────────────────────────────────────────
