"""SpatialSearchMixin — extracted from query_executor_service.py (monolith split, Phase 5)."""

import logging
from django.contrib.gis.measure import D
from django.db.models import FloatField
from typing import Optional
from worldkg_nca.models import OsmEntity
from django.contrib.gis.geos import Point
from django.db.models.expressions import RawSQL
import time

from semantic_search.services.query_executor_service._constants import (
    DEFAULT_NEAR_RADIUS_M,
    _FASTTEXT_AMENITY_DISTANCE_THRESHOLD,
)

logger = logging.getLogger(__name__)


class SpatialSearchMixin:
    """Mixin providing executor methods to QueryExecutorService."""

    @classmethod
    def _search_by_amenity(cls, amenity_type: str, country_code: str,
                           snapshot_date: str, top_k: int = 50,
                           trace: list = None) -> list:
        """Search for OSM entities by amenity tag value.

        Strategy:
          1. Exact tag match (tags__amenity=value)
          2. WorldKG ontology class resolution (wkgs: class → canonical OSM tag)
          3. FastText semantic search fallback (pgvector cosine similarity)

        Uses direct PostGIS/JSONB queries on OsmEntity for steps 1-2,
        then pgvector for step 3.
        """
        if not amenity_type:
            if trace is not None:
                trace.append({
                    "step": "place_search_skipped",
                    "reason": "no OBJECT concept extracted by the parser",
                    "output_count": 0,
                })
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)

        # ── Step 1: Exact amenity tag match ──
        # `tags__contains` renders `@>` (GIN-accelerated); `tags__amenity=`
        # renders `->>` equality and full-scans the partition.
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__contains={"amenity": amenity_type},
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        qs = qs.exclude(geom__isnull=True)[:top_k]

        entities = list(qs)
        if entities:
            results = [cls._entity_to_result(e) for e in entities]
            if trace is not None:
                trace.append({"step": "place_search",
                              "input": amenity_type,
                              "match_type": "exact_tag",
                              "output_count": len(results)})
            return results

        # ── Step 2: WorldKG ontology class resolution ──
        ontology_results = cls._search_by_ontology_class(
            amenity_type, country_code, snapshot_date, top_k, trace
        )
        if ontology_results:
            return ontology_results

        # ── Step 2b: fuzzy correction (Kuhn's Template correction layer) ──
        # A misspelled phrase ("resturant") fails steps 1-2. Correct it
        # against the snapshot's real tag vocabulary (pg_trgm +
        # fuzzystrmatch) and retry the exact-tag tier with the canonical
        # form so the FastText step never sees a garbage embedding.
        from semantic_search.services.query_correction_service import (
            QueryCorrectionService,
        )
        corrected = QueryCorrectionService.correct_amenity(
            amenity_type, country_code=country_code, snapshot_id=snapshot_id,
        )
        if corrected and corrected[0] != amenity_type:
            corrected_tag = corrected[0]
            qs = OsmEntity.objects.using("vectors").filter(
                snapshot_id=snapshot_id,
                tags__contains={"amenity": corrected_tag},
            )
            if country_code:
                qs = qs.filter(country_code=country_code.upper())
            qs = qs.exclude(geom__isnull=True)[:top_k]
            entities = list(qs)
            if entities:
                results = [cls._entity_to_result(e) for e in entities]
                if trace is not None:
                    trace.append({
                        "step": "place_search",
                        "input": amenity_type,
                        "match_type": "fuzzy_correction",
                        "corrected_to": corrected_tag,
                        "similarity": corrected[1],
                        "output_count": len(results),
                    })
                return results

        # ── Step 3: generic amenity phrases ("amenities are") → any entity
        #    asserting an amenity-type key (GIN-indexed `tags ?|` — no
        #    embedding scan).
        generic_keys = cls._generic_amenity_keys(amenity_type)
        if generic_keys:
            qs = OsmEntity.objects.using("vectors").filter(
                snapshot_id=snapshot_id,
                tags__has_any_keys=generic_keys,
            )
            if country_code:
                qs = qs.filter(country_code=country_code.upper())
            qs = qs.exclude(geom__isnull=True)[:top_k]
            entities = list(qs)
            results = [cls._entity_to_result(e) for e in entities]
            if trace is not None:
                trace.append({
                    "step": "place_search",
                    "input": amenity_type,
                    "match_type": "generic_amenity",
                    "output_count": len(results),
                })
            return results

        # ── Step 4: FastText semantic search fallback ──
        fasttext_results = cls._search_by_fasttext(
            amenity_type, country_code, snapshot_date, top_k, trace
        )
        return fasttext_results
    @classmethod
    def _search_by_ontology_class(cls, amenity_type: str, country_code: str,
                                   snapshot_date: str, top_k: int,
                                   trace: list = None) -> list:
        """Resolve amenity string to WorldKG class and search by class hierarchy.

        Maps common amenity names to wkgs: classes, then searches for
        entities matching the class or its subclasses.
        """
        try:
            from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
            ontology = get_worldkg_ontology_service()
        except Exception:
            return []

        # DB-driven resolution (rule 6.2 — hardening Phase 5): the
        # factor_amenity_class_mapping table, populated by
        # compute_amenity_class_mappings (data + ontology tiers).
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )

        wkg_class = FactorResolutionService().amenity_class(amenity_type)
        if not wkg_class:
            return []

        # Get canonical OSM tags for this class
        canonical_tags = ontology.get_canonical_tags(wkg_class)
        if not canonical_tags:
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        qs = qs.exclude(geom__isnull=True)

        # Filter by each canonical tag. `tags__contains` renders `@>`
        # (GIN-accelerated); `tags__key=value` renders `->>` and scans.
        for tag_key, tag_value in canonical_tags.items():
            if tag_value:
                qs = qs.filter(tags__contains={tag_key: tag_value})
            else:
                qs = qs.filter(**{f"tags__has_key": tag_key})

        qs = qs[:top_k]
        entities = list(qs)
        if not entities:
            return []

        results = [cls._entity_to_result(e) for e in entities]
        if trace is not None:
            trace.append({"step": "place_search",
                          "input": amenity_type,
                          "match_type": "ontology_class",
                          "wkg_class": wkg_class,
                          "output_count": len(results)})
        return results
    @classmethod
    def _search_by_fasttext(cls, amenity_type: str, country_code: str,
                             snapshot_date: str, top_k: int,
                             trace: list = None) -> list:
        """FastText semantic search fallback using pgvector cosine similarity.

        Computes a FastText embedding for the amenity query string and
        searches for entities with similar gv_tags_embeddings.

        The query embedding comes from the precomputed AmenityEmbedding
        table when the string is in the MapQA vocabulary
        (FACTOR_NODE_RUNTIME_JOINS_PLAN.md §3.4); runtime FastText is the
        fallback for open-vocabulary strings.
        """
        try:
            from django.db.models import FloatField
            from django.db.models.expressions import RawSQL
            query_embedding = cls._amenity_query_embedding(amenity_type)
        except Exception as exc:
            logger.warning("FastText fallback failed for '%s': %s", amenity_type, exc)
            if trace is not None:
                trace.append({"step": "place_search",
                              "input": amenity_type,
                              "match_type": "fasttext",
                              "error": str(exc),
                              "output_count": 0})
            return []

        query_list = query_embedding.tolist()
        snapshot_id = cls._get_snapshot_id(snapshot_date)

        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            gv_tags_embedding__isnull=False,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        qs = qs.exclude(geom__isnull=True)

        # Annotate with cosine distance (pgvector <=> operator)
        # The + 0 prevents HNSW index usage (exact search, not approximate)
        qs = qs.annotate(
            embedding_distance=RawSQL(
                "(gv_tags_embedding <=> %s::vector) + 0",
                (query_list,),
                output_field=FloatField(),
            )
        ).order_by("embedding_distance")[:top_k]

        try:
            entities = list(qs)
        except Exception as exc:
            logger.warning("FastText pgvector query failed: %s", exc)
            if trace is not None:
                trace.append({"step": "place_search",
                              "input": amenity_type,
                              "match_type": "fasttext",
                              "error": str(exc),
                              "output_count": 0})
            return []

        results = []
        for e in entities:
            r = cls._entity_to_result(e)
            if hasattr(e, "embedding_distance"):
                r["embedding_distance"] = round(float(e.embedding_distance), 4)
            results.append(r)

        # Semantic cutoff — drop pool members whose tags embed is too far
        # from the amenity phrase. The remaining tier logic (radius filter,
        # distance ranking) then operates on a semantically-tight pool
        # instead of whatever the phrase happened to retrieve.
        results = [
            r for r in results
            if r.get("embedding_distance", 0.0) <= _FASTTEXT_AMENITY_DISTANCE_THRESHOLD
        ]

        if trace is not None:
            trace.append({"step": "place_search",
                          "input": amenity_type,
                          "match_type": "fasttext",
                          "threshold": _FASTTEXT_AMENITY_DISTANCE_THRESHOLD,
                          "output_count": len(results)})
        return results
    @classmethod
    def _search_by_amenity_spatial(cls, amenity_type: str, country_code: str,
                                    snapshot_date: str,
                                    anchor_point: Point,
                                    radius_m: int = None,
                                    top_k: int = 50,
                                    trace: list = None) -> list:
        """Search for OSM entities by amenity tag, filtered/ordered by PostGIS.

        Uses ST_DWithin (GiST index-backed) for radius filtering and
        Distance annotation for proximity ordering.

        Has the same 3-tier fallback as _search_by_amenity:
          1. Exact tag match with spatial filter (tags__amenity=value + ST_DWithin)
          2. If no results: ontology class resolution with spatial filter
          3. If no results: FastText semantic search, then filter/rank by
             distance in Python (can't use pgvector + ST_DWithin together
             efficiently)

        This handles natural-language phrases like "italian food" that don't
        match any OSM amenity tag value — the exact match returns zero, and
        the FastText fallback resolves the phrase semantically.
        ([MAPQA_TO_EXECUTION_PLAN:§4.1] — parser extracts the raw phrase,
        executor's data plane resolves it via 3-tier fallback)

        Args:
            amenity_type: Amenity tag value or natural-language phrase
            country_code: ISO 3166-1 alpha-2 code
            snapshot_date: Snapshot partition key
            anchor_point: GeoDjango Point (lon, lat, srid=4326)
            radius_m: Optional radius in meters for ST_DWithin filter
            top_k: Maximum results to return
            trace: Optional execution trace list
        """
        if not amenity_type:
            if trace is not None:
                trace.append({
                    "step": "place_search_skipped",
                    "reason": "no OBJECT concept extracted by the parser",
                    "output_count": 0,
                })
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)

        # ── Step 1: Exact tag match with spatial filter ──
        # `tags__contains` renders `@>` (GIN-accelerated); `tags__amenity=`
        # renders `->>` equality and full-scans the partition.
        # NaN geometries (POINT(NaN NaN) ways — not NULL, so exclude()
        # misses them) must be dropped: ST_Distance on NaN coordinates
        # returns 0, ranking coordinate-less entities as "nearest (0m
        # away)" ahead of real matches.
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__contains={"amenity": amenity_type},
        ).exclude(geom__isnull=True).extra(
            where=["NOT (ST_X(geom) = 'NaN' AND ST_Y(geom) = 'NaN')"],
        )

        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        if radius_m is not None:
            # Use ST_DWithin with geography cast — supports meters on
            # geographic (SRID 4326) columns, unlike D(m=) which requires
            # degree units on geographic fields.
            qs = qs.extra(
                where=["ST_DWithin(geom::geography, ST_MakePoint(%s, %s)::geography, %s)"],
                params=[float(anchor_point.x), float(anchor_point.y), float(radius_m)],
            )

        qs = qs.annotate(
            distance_m=RawSQL(
                "ST_Distance(geom::geography, ST_MakePoint(%s, %s)::geography)",
                (float(anchor_point.x), float(anchor_point.y)),
                output_field=FloatField(),
            )
        ).order_by("distance_m")[:top_k]

        try:
            entities = list(qs)
        except Exception as exc:
            logger.warning("Spatial amenity query failed, falling back: %s", exc)
            entities = []

        if entities:
            results = []
            for e in entities:
                r = cls._entity_to_result(e)
                if hasattr(e, "distance_m"):
                    r["distance_m"] = round(float(e.distance_m), 1)
                results.append(r)
            if trace is not None:
                trace.append({
                    "step": "place_search_spatial",
                    "input": amenity_type,
                    "match_type": "exact_tag",
                    "radius_m": radius_m,
                    "output_count": len(results),
                })
            return results

        # ── Step 2: Ontology class resolution with spatial filter ──
        ontology_results = cls._search_by_ontology_class_spatial(
            amenity_type, country_code, snapshot_date,
            anchor_point, radius_m, top_k, trace
        )
        if ontology_results:
            return ontology_results

        # ── Step 3: generic amenity phrases → key-existence + ST_DWithin ──
        #    (both GIN/GiST-indexed — no embedding scan). A missing radius
        #    defaults to DEFAULT_NEAR_RADIUS_M: the unbounded `?|` pool
        #    (535K rows on IE) would otherwise be fully sorted by distance
        #    (~9s). The FastText tier below remains the unbounded-nearest
        #    fallback (its pool is Python-sorted, capped at ~250).
        generic_keys = cls._generic_amenity_keys(amenity_type)
        if generic_keys:
            if radius_m is None:
                radius_m = DEFAULT_NEAR_RADIUS_M
            qs = OsmEntity.objects.using("vectors").filter(
                snapshot_id=snapshot_id,
                tags__has_any_keys=generic_keys,
            )
            if country_code:
                qs = qs.filter(country_code=country_code.upper())
            qs = qs.exclude(geom__isnull=True)
            qs = qs.extra(
                where=["ST_DWithin(geom::geography, ST_MakePoint(%s, %s)::geography, %s)"],
                params=[float(anchor_point.x), float(anchor_point.y), float(radius_m)],
            )
            qs = qs.annotate(
                distance_m=RawSQL(
                    "ST_Distance(geom::geography, ST_MakePoint(%s, %s)::geography)",
                    (float(anchor_point.x), float(anchor_point.y)),
                    output_field=FloatField(),
                )
            ).order_by("distance_m")[:top_k]
            try:
                entities = list(qs)
            except Exception as exc:  # noqa: BLE001 — fall through to FastText
                logger.warning("Generic amenity spatial query failed: %s", exc)
                entities = []
            if entities:
                results = []
                for e in entities:
                    r = cls._entity_to_result(e)
                    if hasattr(e, "distance_m"):
                        r["distance_m"] = round(float(e.distance_m), 1)
                    results.append(r)
                if trace is not None:
                    trace.append({
                        "step": "place_search_spatial",
                        "input": amenity_type,
                        "match_type": "generic_amenity",
                        "radius_m": radius_m,
                        "output_count": len(results),
                    })
                return results

        # ── Step 4: FastText semantic search, then spatial filter in Python ──
        # Can't combine pgvector <=> with ST_DWithin efficiently, so we
        # fetch a larger pool via FastText and filter by distance in Python.
        fasttext_pool = cls._search_by_fasttext(
            amenity_type, country_code, snapshot_date,
            top_k=max(top_k * 5, 200), trace=trace,
        )
        if not fasttext_pool:
            return []

        # Filter by radius and rank by distance to anchor
        filtered = []
        for r in fasttext_pool:
            r_lat = r.get("lat")
            r_lon = r.get("lon")
            if r_lat is None or r_lon is None:
                continue
            dist_m = cls._haversine_km(
                anchor_point.y, anchor_point.x, r_lat, r_lon
            ) * 1000
            if radius_m is None or dist_m <= radius_m:
                r["distance_m"] = round(dist_m, 1)
                filtered.append(r)

        filtered.sort(key=lambda x: x["distance_m"])
        results = filtered[:top_k]

        if trace is not None:
            trace.append({
                "step": "place_search_spatial",
                "input": amenity_type,
                "match_type": "fasttext+spatial_filter",
                "radius_m": radius_m,
                "pool_size": len(fasttext_pool),
                "output_count": len(results),
            })
        return results
    @classmethod
    def _search_by_ontology_class_spatial(cls, amenity_type: str,
                                           country_code: str,
                                           snapshot_date: str,
                                           anchor_point: Point,
                                           radius_m: int,
                                           top_k: int,
                                           trace: list = None) -> list:
        """Ontology class resolution with spatial filtering."""
        try:
            from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
            ontology = get_worldkg_ontology_service()
        except Exception:
            return []

        amenity_to_wkgs = {
            "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
            "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
            "hotel": "wkgs:Hotel", "resort": "wkgs:Hotel",
            "hospital": "wkgs:Hospital", "clinic": "wkgs:Hospital",
            "school": "wkgs:School", "university": "wkgs:School",
            "shop": "wkgs:Shop", "store": "wkgs:Shop", "mall": "wkgs:Shop",
            "bar": "wkgs:Amenity", "pub": "wkgs:Amenity",
        }
        key = amenity_type.lower().replace(" ", "_")
        wkg_class = amenity_to_wkgs.get(key)
        if not wkg_class:
            return []

        canonical_tags = ontology.get_canonical_tags(wkg_class)
        if not canonical_tags:
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
        ).exclude(geom__isnull=True)

        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        for tag_key, tag_value in canonical_tags.items():
            if tag_value:
                # `tags__contains` renders `@>` (GIN-accelerated);
                # `tags__key=value` renders `->>` and scans.
                qs = qs.filter(tags__contains={tag_key: tag_value})
            else:
                qs = qs.filter(**{f"tags__has_key": tag_key})

        if radius_m is not None:
            qs = qs.extra(
                where=["ST_DWithin(geom::geography, ST_MakePoint(%s, %s)::geography, %s)"],
                params=[float(anchor_point.x), float(anchor_point.y), float(radius_m)],
            )

        qs = qs.annotate(
            distance_m=RawSQL(
                "ST_Distance(geom::geography, ST_MakePoint(%s, %s)::geography)",
                (float(anchor_point.x), float(anchor_point.y)),
                output_field=FloatField(),
            )
        ).order_by("distance_m")[:top_k]
        entities = list(qs)
        if not entities:
            return []

        results = []
        for e in entities:
            r = cls._entity_to_result(e)
            if hasattr(e, "distance_m"):
                r["distance_m"] = round(float(e.distance_m), 1)
            results.append(r)
        if trace is not None:
            trace.append({
                "step": "place_search_spatial",
                "input": amenity_type,
                "match_type": "ontology_class",
                "wkg_class": wkg_class,
                "radius_m": radius_m,
                "output_count": len(results),
            })
        return results

    # ── Graph / spectral / community / event templates ──────────────────────
    # These templates use the factor_* tables (SQL + pgvector) at request time.
    # The legacy runtime graph path (GraphML → NetworkX → SciPy) has been
    # removed.  Batch-side services (KNNGraphService, SpectralAnalysisService,
    # CommunityDetectionService, etc.) remain for Step 5c/5d.
