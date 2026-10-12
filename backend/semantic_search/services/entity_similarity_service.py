"""
EntitySimilarityService — entity-seeded embedding k-NN (the doppelganger
primitive).

"Similar entities to X" = fetch X's stored ``gv_tags_embedding`` and run
one exact cosine (``<=>``) query against the snapshot's embeddings. The
embeddings are the amortized artifact (Step 1, computed once per
entity); similarity search is its cheapest consumer: one pgvector query
per request, no new computation.

This answers "like X", not "near X": proximity is the spatial search's
job (ST_DWithin); similarity is the embedding's. The 2026-10-11 Friars
Tavern run answered the similarity ask with a proximity census (Dick's
Bike Hire at 236m) because no representation for "similar" existed.

Signals, in maturity order:
  1. gv_tags_embedding (300d, FastText of tags) — always populated
     (Step 1); the primary axis here.
  2. gv_nle_embedding (100d, DeepWalk graph position) — Step-5-trained
     countries; a later axis.
  3. factor_spectral_node_metric eigen-loadings cosine — same
     structural role; a later axis (factor_resolution_service).
  4. PrecomputedLinkCandidate (USLP predicted relations) — exists.
"""

import json
import logging
from math import isfinite

from django.db import connections

from semantic_search.services.entity_geocoder import EntityGeocoder
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)


class EntitySimilarityService:
    """Entity-seeded k-NN over the stored tag embeddings."""

    @classmethod
    def similar_entities(cls, entity_name: str, country_code: str = None,
                         snapshot_date: str = None, top_k: int = 10,
                         scope: str = "same_country") -> dict:
        """The entities most similar to ``entity_name`` by tag embedding.

        Args:
            entity_name: the seed entity's name (geocoded first).
            country_code: optional ISO code scoping the seed lookup.
            snapshot_date: optional snapshot partition key.
            top_k: max neighbors (1-50).
            scope: "same_country" (default; the seed's country) or
                "all" (cross-country twins).

        Returns:
            {"seed": {osm_id, osm_type, name, country_code, lat, lon},
             "results": [{name, wkg_class, tags, country_code, lat, lon,
                          similarity}], "count": int}
            or {"error": str} when the seed or its embedding is missing.
        """
        if not entity_name:
            return {"error": "no seed entity name"}
        top_k = max(1, min(int(top_k or 10), 50))

        seed_ref = EntityGeocoder.geocode(
            entity_name, country_code, snapshot_date
        )
        if not seed_ref or seed_ref.get("osm_id") is None:
            return {"error": f"seed entity {entity_name!r} not found"}

        snapshot_id = snapshot_date or get_latest_snapshot_id()
        seed = cls._fetch_seed(snapshot_id, seed_ref)
        if not seed:
            return {"error": f"seed entity {entity_name!r} not found in snapshot"}
        if not seed.get("embedding"):
            return {"error": "seed entity has no tag embedding"}

        rows = cls._knn(
            seed["embedding"], snapshot_id, seed["country_code"],
            seed["osm_type"], seed["osm_id"], top_k, scope,
        )
        results = []
        for row in rows:
            tags = row["tags"] or {}
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except ValueError:
                    tags = {}
            dist = float(row["dist"])
            results.append({
                "name": tags.get("name", ""),
                "wkg_class": row["wkg_class"],
                "tags": tags,
                "country_code": row["country_code"],
                "lat": row["lat"],
                "lon": row["lon"],
                "similarity": round(1.0 - dist, 4),
            })
        return {
            "seed": {
                "osm_id": seed["osm_id"], "osm_type": seed["osm_type"],
                "name": seed["name"], "country_code": seed["country_code"],
                "lat": seed["lat"], "lon": seed["lon"],
            },
            "results": results,
            "count": len(results),
        }

    @classmethod
    def _fetch_seed(cls, snapshot_id, seed_ref) -> dict:
        """The seed row: embedding (as the pgvector text form), country,
        coords. NaN-geometry seeds are rejected (no usable position)."""
        sql = """
            SELECT osm_type, osm_id, tags, wkg_class, country_code,
                   ST_Y(geom) AS lat, ST_X(geom) AS lon,
                   gv_tags_embedding::text AS embedding
            FROM semantic_search_osmentity
            WHERE snapshot_id = %s AND osm_type = %s AND osm_id = %s
            LIMIT 1
        """
        with connections["vectors"].cursor() as cur:
            cur.execute(sql, [snapshot_id, seed_ref["osm_type"],
                             seed_ref["osm_id"]])
            row = cur.fetchone()
        if not row:
            return None
        cols = ["osm_type", "osm_id", "tags", "wkg_class", "country_code",
                "lat", "lon", "embedding"]
        d = dict(zip(cols, row))
        tags = d["tags"]
        if isinstance(tags, str):
            try:
                d["tags"] = json.loads(tags)
            except ValueError:
                d["tags"] = {}
        d["name"] = (d["tags"] or {}).get("name", "")
        lat, lon = d.get("lat"), d.get("lon")
        if lat is not None and not (isfinite(float(lat))
                                    and isfinite(float(lon))):
            d["lat"] = d["lon"] = None
        return d

    @classmethod
    def _knn(cls, embedding_text, snapshot_id, seed_country, seed_type,
             seed_id, top_k, scope) -> list:
        """The exact-cosine k-NN query. Same exact (non-HNSW) form as the
        triplet search: the +0 defeats the planner's index-only scan."""
        country_clause = ""
        params = [embedding_text, snapshot_id, seed_type, seed_id]
        if scope != "all" and seed_country:
            country_clause = "AND country_code = %s"
            params.append(seed_country)
        sql = f"""
            SELECT osm_id, osm_type, tags, wkg_class, country_code,
                   ST_Y(geom) AS lat, ST_X(geom) AS lon,
                   (gv_tags_embedding <=> %s::vector) AS dist
            FROM semantic_search_osmentity
            WHERE snapshot_id = %s
              AND gv_tags_embedding IS NOT NULL
              AND NOT (osm_type = %s AND osm_id = %s)
              {country_clause}
              AND geom IS NOT NULL
              AND NOT (ST_X(geom) = 'NaN'::float8
                       AND ST_Y(geom) = 'NaN'::float8)
            ORDER BY gv_tags_embedding <=> %s::vector
            LIMIT %s
        """
        params += [embedding_text, top_k]
        with connections["vectors"].cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
