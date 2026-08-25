"""
QueryExecutorService — execute parsed GeoFlow Graphs against the data plane.

Each of the 5 macro-templates has a dedicated execution function that:
  1. Resolves concept nodes (geocodes entity names → coordinates)
  2. Applies operators in role-precedence order (SUB_COND → MEASURE)
  3. Records an execution trace for grounded answer synthesis

The executor calls existing backend services (FastText search, PostGIS
queries, OsmEntity lookups). No new data-plane infrastructure.

Implements MAPQA_TO_EXECUTION_PLAN.md §2.2.
"""

import logging
import math
import re
import time
from typing import Optional

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.gis.db.models.functions import Distance as GisDistance
from django.db import connection
from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from semantic_search.services.entity_geocoder import EntityGeocoder
from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)

# ── USLP geographic scoring (Mann et al. 2023 §3.3) ───────────────────────
# The paper's geo_score uses geohash cluster centers at relation-specific
# precision levels, with d_max = per-tail-cluster max distance to any other
# cluster center at that precision (the per-column max of the distance matrix).
#
# Geohash precision cell widths (geohash2 reference):
#   P1: ~5000 km  — country/continent level (isInCountry, capitalCity)
#   P3: ~156 km   — state/county/district level (isInCounty, addrState)
#   P4: ~39 km    — local level (addrSuburb, addrHamlet, addrCity)
#
# For MapQA templates, we use P4 (local) as the default precision since most
# queries are local-scale. FILTER-AGGREGATE-MEASURE uses the user's explicit
# radius as d_max with raw haversine (no geohash). OBJECT-FIELD-MEASURE has
# no geo_score (distance IS the answer).
#
# d_max fallback when no pool is loaded: the geohash cell width at the
# precision level (P4 ≈ 39 km). The paper computes d_max from the candidate
# pool's cluster centers; at runtime without a precomputed pool, the cell
# width is the closest approximation.
USLP_GEOHASH_PRECISION = 4
USLP_FALLBACK_D_MAX_KM = {
    1: 5000.0,
    3: 156.0,
    4: 39.0,
}

# Semantic cutoff for the FastText amenity fallback: keep only entities whose
# gv_tags embedding is within this cosine distance of the amenity phrase.
# Without it the tier returns the NEAREST entities of whatever pool the phrase
# retrieved — e.g. for "bus_station" it surfaced shops near the anchor instead
# of admitting there were no bus stations in range. 0.5 matches the
# name-search default (worldkg_nca.views.search name_distance_threshold).
_FASTTEXT_AMENITY_DISTANCE_THRESHOLD = 0.5


class QueryExecutorService:
    """Execute a parsed query against the data plane.

    Routes to one of 5 template-specific execution functions.
    Each function uses existing backend services — no new data plane.
    """

    _executors = None

    @classmethod
    def _get_executors(cls):
        if cls._executors is None:
            cls._executors = {
                "GEOCODE-BATCH-COMPARE (#4)": cls._execute_geocode_batch_compare,
                "FILTER-AGGREGATE-MEASURE (#1)": cls._execute_filter_aggregate_measure,
                "PLACE-ATTRIBUTE-QUERY (#8)": cls._execute_place_attribute_query,
                "LOCATION-BEARING-CLASSIFY (#5)": cls._execute_location_bearing_classify,
                "OBJECT-FIELD-MEASURE (#2)": cls._execute_object_field_measure,
                # New graph/spectral templates (GRAPH_SPECTRAL_TEMPORAL_PLAN.md Phase 4)
                "SPECTRAL-ANALYSIS (#11)": cls._execute_spectral_analysis,
                "TEMPORAL-DRIFT (#12)": cls._execute_temporal_drift,
                "COMMUNITY-DETECT (#13)": cls._execute_community_detect,
                "EVENT-DIFFUSION (#14)": cls._execute_event_diffusion,
            }
        return cls._executors

    @classmethod
    def execute(cls, parsed: dict, country_code: str = None,
                snapshot_date: str = None, question: str = None,
                event_callback=None) -> dict:
        """Execute a parsed query and return results + execution trace.

        Args:
            parsed: {template, concepts, roles, dag, confidence, validation}
                    from QueryParserService.parse()
            country_code: ISO 3166-1 alpha-2 code
            snapshot_date: Optional YYYY_MM_DD snapshot filter
            question: Original question text (used for multi-entity extraction
                      in templates that need 2+ entities)
            event_callback: Optional progress callback for SSE streaming —
                receives {"event": "executed", ...} and enrichment events.

        Returns:
            {template, results, answer, trace, latency_ms}
        """
        start = time.monotonic()
        template = parsed["template"]
        concepts = parsed["concepts"]

        # For multi-entity templates, supplement concepts with full entity extraction
        if question and template in (
            "OBJECT-FIELD-MEASURE (#2)",
            "LOCATION-BEARING-CLASSIFY (#5)",
            "GEOCODE-BATCH-COMPARE (#4)",
        ):
            from semantic_search.services.query_parser_service import QueryParserService
            all_entities = QueryParserService.extract_all_entities(question)
            existing_locations = cls._get_concepts_by_type(concepts, "LOCATION")
            if len(all_entities) >= 2 and len(existing_locations) < 2:
                # Replace/augment LOCATION concepts with full extraction
                concepts = [c for c in concepts if c["type"] != "LOCATION"]
                for entity_name in all_entities:
                    concepts.append({
                        "type": "LOCATION",
                        "text": entity_name,
                        "confidence": 1.0,
                        "resolved_value": None,
                    })

        executors = cls._get_executors()
        executor = executors.get(template)
        if not executor:
            return {"error": f"Unknown template: {template}", "template": template}

        trace = []
        try:
            results = executor(concepts, country_code, snapshot_date, trace)

            # Enrich results with augmented data (IGEA links, USLP predictions)
            if isinstance(results, list) and results:
                results = cls._enrich_results(results, trace)

            # When a question is present the enrichment research loop is the
            # LLM synthesis step — skip the standalone LLM pass here to avoid
            # paying for two answer rewrites (one redundant LLM call).
            answer = cls._synthesize_answer(
                template, concepts, results, trace, skip_llm=bool(question),
            )

            if event_callback:
                event_callback({
                    "event": "executed",
                    "template": template,
                    "result_count": len(results) if isinstance(results, list) else 0,
                    "trace": trace,
                })

            # LLM-driven enrichment: the platform LLM selects research tools
            # (nameSearch / structuredSearch), they execute deterministically,
            # and the LLM synthesizes an enriched answer. Fail-soft — the
            # templated answer is kept on any failure (see
            # query_enrichment_service.py).
            enrichment = None
            if question:
                from semantic_search.services.query_enrichment_service import (
                    QueryEnrichmentService,
                )
                enrichment = QueryEnrichmentService.enrich(
                    question, template, concepts, results,
                    country_code, snapshot_date,
                    event_callback=event_callback,
                )
                if enrichment and enrichment.get("enriched_answer"):
                    answer = enrichment["enriched_answer"]

            latency_ms = (time.monotonic() - start) * 1000

            logger.info("mapqa_execute", extra={
                "template": template,
                "country_code": country_code,
                "result_count": len(results) if isinstance(results, list) else 1,
                "latency_ms": round(latency_ms, 1),
                "enriched": bool(enrichment),
            })

            return {
                "template": template,
                "results": results,
                "answer": answer,
                "trace": trace,
                "latency_ms": round(latency_ms, 1),
                "enrichment": enrichment,
            }
        except Exception as exc:
            logger.exception("mapqa_execute_error", extra={
                "template": template,
                "country_code": country_code,
            })
            return {"error": str(exc), "template": template, "trace": trace}

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _get_concept(concepts: list, ctype: str) -> dict:
        for c in concepts:
            if c["type"] == ctype:
                return c
        return None

    @staticmethod
    def _get_concepts_by_type(concepts: list, ctype: str) -> list:
        return [c for c in concepts if c["type"] == ctype]

    # ── Factor-node tables (FACTOR_NODE_RUNTIME_JOINS_PLAN.md) ────────────
    # The table path is authoritative; the legacy runtime graph path
    # (GraphML → NetworkX → SciPy) has been removed.  PostGIS remains as
    # a spatial fallback for templates without factor-table coverage.

    @classmethod
    def _amenity_query_embedding(cls, amenity_type: str):
        """Query-side amenity embedding: precomputed row first, runtime
        FastText only for out-of-vocabulary strings (the parser's OBJECT
        extraction is open-vocabulary)."""
        try:
            from semantic_search.services.factor_resolution_service import (
                FactorResolutionService,
            )
            emb = FactorResolutionService().amenity_embedding(amenity_type)
            if emb is not None:
                return emb
        except Exception as exc:
            logger.warning(
                "AmenityEmbedding lookup failed for '%s': %s", amenity_type, exc,
            )
        from semantic_search.services.fasttext_service import (
            FastTextEmbeddingService,
        )
        return FastTextEmbeddingService.calculate_text_embedding(amenity_type)

    @classmethod
    def _amenity_candidate_osm_ids(cls, amenity_type: str, country_code: str,
                                   snapshot_id: str, cap: int = 5000):
        """DB-side amenity candidates for the factor-table diffusion path.

        Exact-tag → ontology-class tiers, returns plain osm_id lists
        (the factor table *is* the graph).
        Returns (osm_ids, match_type).
        """
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__amenity=amenity_type,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        ids = list(qs.values_list('osm_id', flat=True)[:cap])
        if ids:
            return ids, "exact_tag"

        amenity_to_wkgs = {
            "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
            "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
            "hotel": "wkgs:Hotel", "hospital": "wkgs:Hospital",
            "school": "wkgs:School", "bar": "wkgs:Amenity",
            "pub": "wkgs:Amenity", "fuel": "wkgs:Amenity",
        }
        wkg_class = amenity_to_wkgs.get(amenity_type.lower().replace(" ", "_"))
        if wkg_class:
            qs = OsmEntity.objects.using("vectors").filter(
                snapshot_id=snapshot_id,
                wkg_class=wkg_class,
            )
            if country_code:
                qs = qs.filter(country_code=country_code.upper())
            ids = list(qs.values_list('osm_id', flat=True)[:cap])
            if ids:
                return ids, "ontology_class"
        return [], None

    @classmethod
    def _heat_kernel_search_via_tables(cls, anchor_osm_id, amenity_type,
                                       country_code, snapshot_date, trace,
                                       t=1.0, top_k=20):
        """Table-path heat kernel: candidates join + pgvector diffusion rank.

        Returns result list or None when the factor tables can't answer
        (caller falls back to PostGIS).
        """
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )

        snapshot_id = cls._get_snapshot_id(snapshot_date)
        candidates, match_type = cls._amenity_candidate_osm_ids(
            amenity_type, country_code, snapshot_id,
        )
        if not candidates:
            return None

        ranked = FactorResolutionService().diffusion_rank(
            anchor_osm_id, t, snapshot_id, country_code,
            candidate_osm_ids=candidates, limit=top_k, trace=trace,
        )
        if ranked is None:
            return None

        entities = {
            e.osm_id: e
            for e in OsmEntity.objects.using("vectors").filter(
                osm_id__in=[r["osm_id"] for r in ranked],
                snapshot_id=snapshot_id,
            )
        }
        results = []
        for r in ranked:
            entity = entities.get(r["osm_id"])
            if entity is None:
                continue
            d = cls._entity_to_result(entity)
            d["diffusion_score"] = round(r["score"], 6)
            results.append(d)

        if trace is not None:
            trace.append({
                "step": "heat_kernel",
                "source": "factor_tables",
                "anchor_node": anchor_osm_id,
                "t": t,
                "amenity": amenity_type,
                "match_type": match_type,
                "total_amenity_nodes": len(candidates),
                "output_count": len(results),
            })
        return results

    @classmethod
    def _event_diffusion_via_tables(cls, source_osm_id, t_values, snap,
                                    country_code, trace):
        """Table-path event diffusion: one pgvector query per t value.

        Returns the same result shape as the graph path, or None when the
        factor tables can't answer (caller falls back).
        """
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )

        frs = FactorResolutionService()
        affected = {}
        for t in t_values:
            ranked = frs.diffusion_rank(
                source_osm_id, t, snap, country_code,
                limit=20, trace=trace,
            )
            if ranked is None:
                return None
            affected[t] = ranked
        return {
            "source_osm_id": source_osm_id,
            "t_values": t_values,
            "affected": affected,
        }

    @classmethod
    def _community_detect_via_tables(cls, concepts, country_code,
                                     snapshot_date, trace):
        """Table-path community detection: GROUP BY over stored Louvain IDs.

        Returns a result dict compatible with the graph path (modularity is
        not stored in the factor tables and is reported as None), or None
        when no factor rows exist (caller falls back).
        """
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )

        snap = cls._get_snapshot_id(snapshot_date)
        obj = cls._get_concept(concepts, "OBJECT")
        wkg_class = None
        if obj and obj.get("text"):
            wkg_class = obj["text"]
            if not wkg_class.startswith("wkgs:"):
                wkg_class = "wkgs:" + wkg_class.capitalize()

        summary = FactorResolutionService().community_summary(
            snap, country_code, wkg_class=wkg_class, trace=trace,
        )
        if summary is None:
            return None

        result = {
            "community_count": summary["community_count"],
            "modularity": None,
            "source": "factor_tables",
        }
        if wkg_class:
            result["filtered_communities"] = summary["communities"]
            result["target_class"] = wkg_class
        return result

    @staticmethod
    def _get_snapshot_id(snapshot_date: str = None) -> str:
        return snapshot_date or get_latest_snapshot_id()

    @staticmethod
    def _parse_radius(text: str) -> int:
        """Parse a radius string like '50m', '2km', '100m' → meters (int)."""
        if not text:
            return None
        m = re.search(r"(\d+)\s*(km|m)?", text, re.IGNORECASE)
        if not m:
            return None
        value = int(m.group(1))
        unit = (m.group(2) or "m").lower()
        return value * 1000 if unit == "km" else value

    @classmethod
    def _enrich_results(cls, results: list, trace: list = None) -> list:
        """Enrich executor results with augmented data (IGEA links, USLP).

        Post-processing step — adds an 'augmented' field to each result
        if augmented data is available. Non-fatal: if enrichment fails,
        the original results are returned unchanged.
        """
        try:
            from api.services.augmented_data_service import AugmentedDataService
        except ImportError:
            return results

        snapshot_id = cls._get_snapshot_id()
        enriched_count = 0
        for r in results:
            osm_id = r.get("osm_id")
            osm_type = r.get("osm_type")
            if not osm_id or not osm_type:
                continue
            try:
                augmented = AugmentedDataService.enrich_entity(
                    osm_id, osm_type, snapshot_id
                )
                if augmented:
                    r["augmented"] = augmented
                    enriched_count += 1
            except Exception:
                pass  # non-fatal

        if trace is not None and enriched_count > 0:
            trace.append({"step": "augmented_enrichment",
                          "output_count": enriched_count})
        return results

    @staticmethod
    def _entity_to_result(entity: OsmEntity) -> dict:
        tags = entity.tags or {}
        lat = lon = None
        if entity.geom:
            lat = entity.geom.y
            lon = entity.geom.x
        return {
            "osm_id": entity.osm_id,
            "osm_type": entity.osm_type,
            "name": tags.get("name", ""),
            "tags": tags,
            "lat": lat,
            "lon": lon,
            "wkg_class": entity.wkg_class,
        }

    # ── Template 1: GEOCODE-BATCH-COMPARE (#4) ──────────────────────────────
    # "Which X is nearest to Y?" / "Which is closer to Y: X1, X2, or X3?"
    #
    # Graph-grounded execution ([SPATIAL_AGENT:§3.5]):
    #   1. SUPPORT: geocode the anchor → find its node in the k-NN graph
    #   2. SUB_COND: Dijkstra single-source shortest path from anchor
    #   3. Filter by amenity type, rank by graph distance (network proximity)
    #   4. MEASURE: report top-k nearest by graph distance

    @classmethod
    def _execute_geocode_batch_compare(cls, concepts, country_code,
                                       snapshot_date, trace):
        amenity = cls._get_concept(concepts, "OBJECT")
        locations = cls._get_concepts_by_type(concepts, "LOCATION")

        # ── Pattern 2: "Which is closer to Y: X1 or X2?" ──
        if len(locations) >= 3 and not amenity:
            return cls._execute_compare_closer(
                locations, country_code, snapshot_date, trace
            )

        # ── Pattern 1: "Which X is nearest to Y?" ──
        anchor = locations[0] if locations else None
        amenity_text = amenity["text"] if amenity else None

        # 1. SUPPORT: geocode the anchor
        anchor_coords = None
        if anchor and anchor["text"]:
            anchor_coords = EntityGeocoder.geocode(
                anchor["text"], country_code, snapshot_date
            )
            trace.append({"step": "geocode", "input": anchor["text"],
                          "output": anchor_coords})

        # 2. PostGIS Distance ordering (graph path removed — factor tables
        #    don't cover this template; PostGIS is the primary path)
        if anchor_coords and anchor_coords.get("lat"):
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            entities = cls._search_by_amenity_spatial(
                amenity_text, country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=None, top_k=50, trace=trace,
            )
            if entities:
                trace.append({"step": "rank_by_distance",
                              "anchor": anchor_coords.get("name"),
                              "top": entities[0].get("name")})
            return entities

        # No anchor — return unfiltered amenity search
        entities = cls._search_by_amenity(
            amenity_text, country_code, snapshot_date, top_k=50, trace=trace,
        )
        return entities

    @classmethod
    def _execute_compare_closer(cls, locations, country_code,
                                 snapshot_date, trace):
        """Handle 'Which is closer to Y: X1 or X2?' pattern.

        Geocodes each named candidate and the anchor, then ranks
        candidates by distance to the anchor.
        """
        # Last location is the anchor, rest are candidates
        anchor_name = locations[-1]["text"]
        candidate_names = [loc["text"] for loc in locations[:-1]]

        anchor = EntityGeocoder.geocode(anchor_name, country_code, snapshot_date)
        trace.append({"step": "geocode", "input": anchor_name,
                      "output": anchor})

        if not anchor or not anchor.get("lat"):
            trace.append({"step": "compare_closer",
                          "error": "could not geocode anchor"})
            return []

        anchor_point = Point(anchor["lon"], anchor["lat"], srid=4326)
        results = []
        for name in candidate_names:
            entity = EntityGeocoder.geocode(name, country_code, snapshot_date)
            if entity and entity.get("lat"):
                dist_m = cls._haversine_m(
                    anchor["lat"], anchor["lon"],
                    entity["lat"], entity["lon"],
                )
                entity["distance_m"] = round(dist_m, 1)
                results.append(entity)

        results.sort(key=lambda x: x.get("distance_m") or float("inf"))
        trace.append({"step": "compare_closer",
                      "candidates": candidate_names,
                      "anchor": anchor_name,
                      "output_count": len(results)})
        return results

    # ── Template 2: FILTER-AGGREGATE-MEASURE (#1) ───────────────────────────
    # "Which bars are within 50m of Hollywood Blvd?"
    #
    # Graph-grounded execution ([SPATIAL_AGENT:§3.5] — operators act on the
    # graph manifold):
    #   1. SUPPORT: geocode the anchor → find its node in the k-NN graph
    #   2. SUB_COND + COND: BFS from anchor node, pruned by radius
    #      (convert radius_m to graph edge-weight threshold), filter by
    #      amenity type
    #   3. MEASURE: count + return matching entities

    @classmethod
    def _execute_filter_aggregate_measure(cls, concepts, country_code,
                                          snapshot_date, trace):
        amenity = cls._get_concept(concepts, "OBJECT")
        radius_concept = cls._get_concept(concepts, "AMOUNT")
        anchor = cls._get_concept(concepts, "LOCATION")

        radius_m = cls._parse_radius(radius_concept["text"] if radius_concept else None)
        amenity_text = amenity["text"] if amenity else None

        # 1. SUPPORT: geocode the anchor
        anchor_coords = None
        if anchor and anchor["text"]:
            anchor_coords = EntityGeocoder.geocode(
                anchor["text"], country_code, snapshot_date
            )
            trace.append({"step": "geocode", "input": anchor["text"],
                          "output": anchor_coords})

        # 2. PostGIS spatial search (graph path removed — factor tables
        #    don't cover this template; PostGIS is the primary path)
        if radius_m and anchor_coords and anchor_coords.get("lat") is not None:
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            entities = cls._search_by_amenity_spatial(
                amenity_text, country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=radius_m, top_k=200, trace=trace,
            )
            return entities

        # 3. Multi-anchor fallback: the anchor text may be a generic
        #    amenity category (e.g. "schools", "hospitals") rather than
        #    a specific named place.  In that case, find all entities of
        #    that category and search for the target amenity within
        #    radius of ANY of them.
        if radius_m and anchor and anchor["text"]:
            multi_results = cls._multi_anchor_amenity_search(
                amenity_text, anchor["text"],
                country_code, snapshot_date,
                radius_m=radius_m, top_k=200, trace=trace,
            )
            if multi_results is not None:
                return multi_results

        # Anchor has no usable coordinates and is not a category.
        # Returning all matching amenities would be misleading (the user
        # asked for a spatial filter), so surface a clear error instead.
        if radius_m and anchor:
            trace.append({
                "step": "spatial_filter_skipped",
                "warning": (
                    "anchor has no coordinates; cannot apply radius filter"
                ),
            })
            return []

        # No anchor or radius — return unfiltered amenity search
        entities = cls._search_by_amenity(
            amenity_text, country_code, snapshot_date, top_k=200, trace=trace,
        )
        return entities

    @classmethod
    def _multi_anchor_amenity_search(cls, amenity_text, anchor_text,
                                      country_code, snapshot_date,
                                      radius_m, top_k, trace):
        """Search for ``amenity_text`` within ``radius_m`` of ANY entity
        matching ``anchor_text`` as an amenity category.

        This handles queries like "cafes within 3km of schools" where
        "schools" is a category, not a specific named place.  Returns
        ``None`` if the anchor text is not a recognisable amenity
        category (so the caller can fall through to the error path).
        """
        # Resolve anchor_text to an amenity tag value via the same 3-tier
        # fallback used for OBJECT concepts.
        anchor_amenity = cls._resolve_amenity_tag(anchor_text)
        if anchor_amenity is None:
            return None

        trace.append({
            "step": "multi_anchor_resolve",
            "input": anchor_text,
            "resolved_amenity": anchor_amenity,
        })

        # Find all anchor entities (e.g. all schools) with coordinates
        anchor_entities = cls._search_by_amenity(
            anchor_amenity, country_code, snapshot_date,
            top_k=500, trace=None,  # silent — logged above
        )
        if not anchor_entities:
            trace.append({
                "step": "multi_anchor_empty",
                "warning": f"no entities found for category '{anchor_amenity}'",
            })
            return []

        trace.append({
            "step": "multi_anchor_anchors",
            "anchor_count": len(anchor_entities),
        })

        # For each anchor, search for the target amenity within radius.
        # Deduplicate by osm_id (a cafe near two schools should appear
        # once, with the distance to the nearest school).
        seen = {}
        for anchor_ent in anchor_entities:
            alat, alon = anchor_ent.get("lat"), anchor_ent.get("lon")
            if alat is None or alon is None:
                continue
            anchor_point = Point(alon, alat, srid=4326)
            hits = cls._search_by_amenity_spatial(
                amenity_text, country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=radius_m, top_k=top_k, trace=None,
            )
            for hit in hits:
                osm_id = hit.get("osm_id")
                dist = hit.get("distance_m", float("inf"))
                if osm_id not in seen or dist < seen[osm_id]["distance_m"]:
                    seen[osm_id] = hit
                    seen[osm_id]["distance_m"] = dist

        results = sorted(seen.values(), key=lambda r: r.get("distance_m", float("inf")))[:top_k]
        trace.append({
            "step": "multi_anchor_search",
            "input": amenity_text,
            "anchor_category": anchor_amenity,
            "anchor_count": len(anchor_entities),
            "radius_m": radius_m,
            "output_count": len(results),
        })
        return results

    @classmethod
    def _resolve_amenity_tag(cls, text):
        """Resolve a free-text phrase to an OSM amenity tag value.

        Returns the amenity tag value (e.g. "school") if the text maps
        to a known amenity, or ``None`` if it doesn't look like an
        amenity category (so the caller can treat it as a place name).
        """
        if not text:
            return None
        key = text.lower().strip().rstrip("s")  # singularise
        # Direct amenity tag match
        from worldkg_nca.models import OsmEntity
        from worldkg_nca.snapshot_utils import get_latest_snapshot_id
        snapshot_id = cls._get_snapshot_id(None)
        exists = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__amenity=key,
        ).exists()
        if exists:
            return key
        # Ontology class mapping
        amenity_to_wkgs = {
            "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
            "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
            "hotel": "wkgs:Hotel", "resort": "wkgs:Hotel",
            "hospital": "wkgs:Hospital", "clinic": "wkgs:Hospital",
            "school": "wkgs:School", "university": "wkgs:School",
            "shop": "wkgs:Shop", "store": "wkgs:Shop", "mall": "wkgs:Shop",
            "bar": "wkgs:Amenity", "pub": "wkgs:Amenity",
        }
        if key in amenity_to_wkgs:
            return key
        return None

    # ── Template 3: PLACE-ATTRIBUTE-QUERY (#8) ──────────────────────────────
    # "What restaurant is near X?" / "italian food near a bus station"
    #
    # Factor-table execution ([SPATIAL_AGENT:§3.5]):
    #   1. SUPPORT: geocode the anchor → resolve its osm_id
    #   2. SUB_COND: heat kernel diffusion via pgvector <#> against stored
    #      eigen-loadings — one SQL query, no NetworkX/SciPy at request time
    #   3. Filter diffused nodes by amenity type, rank by diffusion score
    #   4. MEASURE: report top-k results
    #   PostGIS spatial search is the fallback when factor tables can't answer.

    @classmethod
    def _execute_place_attribute_query(cls, concepts, country_code,
                                       snapshot_date, trace):
        object_concept = cls._get_concept(concepts, "OBJECT")
        location_concept = cls._get_concept(concepts, "LOCATION")
        amount_concept = cls._get_concept(concepts, "AMOUNT")

        amenity_type = object_concept["text"] if object_concept else None
        anchor_name = location_concept["text"] if location_concept else None
        radius_m = cls._parse_radius(amount_concept["text"]) if amount_concept else None

        if not amenity_type and not anchor_name:
            trace.append({"step": "place_search",
                          "error": "no amenity type or anchor name"})
            return []

        # Geocode the anchor
        anchor_coords = None
        anchor_osm_id = None
        if anchor_name:
            anchor_coords = EntityGeocoder.geocode(
                anchor_name, country_code, snapshot_date
            )
            if trace is not None:
                trace.append({"step": "geocode_anchor",
                              "input": anchor_name,
                              "output": anchor_coords})
            if anchor_coords:
                anchor_osm_id = anchor_coords.get("osm_id")

        # Factor-table path (FACTOR_NODE_RUNTIME_JOINS_PLAN.md): SQL-only
        # heat-kernel ranking via stored eigen-loadings.
        if (country_code and anchor_osm_id and amenity_type):
            table_results = cls._heat_kernel_search_via_tables(
                anchor_osm_id, amenity_type, country_code, snapshot_date, trace,
            )
            if table_results is not None:
                # Add geo_score + USLP boost to each result, then re-rank.
                template = "PLACE-ATTRIBUTE-QUERY (#8)"
                table_results = cls._enrich_with_geo_and_uslp(
                    table_results, template, anchor_coords, radius_m,
                    anchor_osm_id, country_code, snapshot_date, trace,
                )
                # Geographic radius guard — if an AMOUNT (radius) concept was
                # parsed, filter heat-kernel results by haversine distance.
                # Heat kernel diffusion respects graph connectivity, not
                # geographic distance, so entities far away in km can still
                # get high diffusion scores.
                if radius_m and anchor_coords and anchor_coords.get("lat"):
                    filtered = []
                    for r in table_results:
                        if r.get("lat") is not None and r.get("lon") is not None:
                            dist_m = cls._haversine_m(
                                anchor_coords["lat"], anchor_coords["lon"],
                                r["lat"], r["lon"],
                            )
                            if dist_m <= radius_m:
                                filtered.append(r)
                    if trace is not None:
                        trace.append({
                            "step": "radius_guard",
                            "radius_m": radius_m,
                            "before": len(table_results),
                            "after": len(filtered),
                        })
                    return filtered
                return table_results
            if trace is not None:
                trace.append({"step": "factor_join",
                              "warning": "table path unavailable — falling back"})

        # Fallback: PostGIS spatial search when factor tables unavailable
        if anchor_coords and anchor_coords.get("lat") and amenity_type:
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            entities = cls._search_by_amenity_spatial(
                amenity_type, country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=2000, top_k=20, trace=trace,
            )
            return entities

        # Only amenity type, no anchor or geocode failed
        if amenity_type:
            return cls._search_by_amenity(
                amenity_type, country_code, snapshot_date,
                top_k=20, trace=trace,
            )

        # Only anchor name, no amenity type — search by name
        snapshot_id = cls._get_snapshot_id(snapshot_date)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__name__icontains=anchor_name,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        entities = list(qs[:10])
        if trace is not None:
            trace.append({"step": "place_search", "input": anchor_name,
                          "output_count": len(entities)})

        results = [cls._entity_to_result(e) for e in entities]
        if trace is not None:
            trace.append({"step": "place_details",
                          "output_count": len(results)})
        return results

    # ── Template 4: LOCATION-BEARING-CLASSIFY (#5) ──────────────────────────
    # "Which direction is X from Y?"

    @classmethod
    def _execute_location_bearing_classify(cls, concepts, country_code,
                                            snapshot_date, trace):
        from semantic_search.services.query_parser_service import QueryParserService

        locations = cls._get_concepts_by_type(concepts, "LOCATION")
        # If only one LOCATION concept was extracted, try multi-entity extraction
        if len(locations) < 2:
            # Re-extract from the first concept's source question
            # (stored in concept text — we need the original question)
            # Use the entity names we have + try to get more
            trace.append({"step": "batch_geocode",
                          "error": "need 2 LOCATION concepts, got %d" % len(locations)})
            # Try extracting from the concept texts directly
            if len(locations) == 1 and locations[0]["text"]:
                # Single entity — can't compute bearing without a second
                return [{"error": "Need two locations to compute bearing"}]
            return []

        a = EntityGeocoder.geocode(locations[0]["text"], country_code, snapshot_date)
        b = EntityGeocoder.geocode(locations[1]["text"], country_code, snapshot_date)
        trace.append({"step": "batch_geocode",
                      "inputs": [locations[0]["text"], locations[1]["text"]],
                      "outputs": [a, b]})

        if not a or not b or not a.get("lat") or not b.get("lat"):
            return [{"error": "Could not geocode one or both entities"}]

        # Bearing ([SPATIAL_AGENT:§C.4] — Equation 8)
        phi1, lam1 = math.radians(a["lat"]), math.radians(a["lon"])
        phi2, lam2 = math.radians(b["lat"]), math.radians(b["lon"])
        dlam = lam2 - lam1
        y = math.sin(dlam) * math.cos(phi2)
        x = (math.cos(phi1) * math.sin(phi2) -
             math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
        theta = (math.degrees(math.atan2(y, x)) + 360) % 360
        trace.append({"step": "bearing", "output_degrees": round(theta, 1)})

        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        direction = directions[round(theta / 45) % 8]
        trace.append({"step": "bearing_to_direction",
                      "input_degrees": round(theta, 1),
                      "output": direction})

        return [{"bearing_degrees": round(theta, 1), "direction": direction,
                 "from": locations[0]["text"], "to": locations[1]["text"]}]

    # ── Template 5: OBJECT-FIELD-MEASURE (#2) ───────────────────────────────
    # "How far is X from Y?"
    #
    # Graph-grounded execution ([SPATIAL_AGENT:§3.5]):
    #   1. SUB_COND + COND: geocode both locations → find their nodes in graph
    #   2. SUPPORT: Dijkstra shortest path between the two nodes
    #   3. MEASURE: report graph distance + geographic distance

    @classmethod
    def _execute_object_field_measure(cls, concepts, country_code,
                                      snapshot_date, trace):
        locations = cls._get_concepts_by_type(concepts, "LOCATION")
        if len(locations) < 2:
            trace.append({"step": "batch_geocode",
                          "error": "need 2 LOCATION concepts, got %d" % len(locations)})
            if len(locations) == 1 and locations[0]["text"]:
                return [{"error": "Need two locations to compute distance"}]
            return []

        a = EntityGeocoder.geocode(locations[0]["text"], country_code, snapshot_date)
        b = EntityGeocoder.geocode(locations[1]["text"], country_code, snapshot_date)
        trace.append({"step": "batch_geocode",
                      "inputs": [locations[0]["text"], locations[1]["text"]],
                      "outputs": [a, b]})

        if not a or not b or not a.get("lat") or not b.get("lat"):
            return [{"error": "Could not geocode one or both entities"}]

        # Geographic distance (haversine) — primary metric (graph path removed)
        geo_dist_km = cls._haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        geo_dist_m = geo_dist_km * 1000

        trace.append({"step": "haversine",
                      "output_km": round(geo_dist_km, 3),
                      "output_m": round(geo_dist_m, 1)})

        result = {"distance_km": round(geo_dist_km, 3),
                  "distance_m": round(geo_dist_m, 1),
                  "from": locations[0]["text"],
                  "to": locations[1]["text"],
                  "from_coords": {"lat": a["lat"], "lon": a["lon"]},
                  "to_coords": {"lat": b["lat"], "lon": b["lon"]}}
        return [result]

    # ── Data-plane search helper ────────────────────────────────────────────

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
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__amenity=amenity_type,
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

        # ── Step 3: FastText semantic search fallback ──
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

        # Map amenity string to wkgs: class name
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

        # Filter by each canonical tag
        for tag_key, tag_value in canonical_tags.items():
            if tag_value:
                qs = qs.filter(**{f"tags__{tag_key}": tag_value})
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
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__amenity=amenity_type,
        ).exclude(geom__isnull=True)

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

        # ── Step 3: FastText semantic search, then spatial filter in Python ──
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
                qs = qs.filter(**{f"tags__{tag_key}": tag_value})
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

    @classmethod
    def _execute_spectral_analysis(cls, concepts, country_code,
                                   snapshot_date, trace):
        """SPECTRAL-ANALYSIS — Laplacian spectral features for a region/snapshot.

        Reads the cached GraphSpectralFingerprint from Step 5c.  The
        on-the-fly graph computation path has been removed — Step 5c must
        have run for this template to answer.
        """
        from semantic_search.models import GraphSpectralFingerprint
        from osmsnapshot.models import Snapshot
        snap = cls._get_snapshot_id(snapshot_date)
        snapshot = (
            Snapshot.objects.using('default')
            .filter(country_code__iexact=country_code, snapshot_date=snap)
            .order_by('-created_at').first()
        ) if country_code else None
        if snapshot is not None:
            fp = (
                GraphSpectralFingerprint.objects
                .filter(region=country_code, snapshot=snapshot)
                .order_by('-created_at').first()
            )
            if fp is not None:
                if trace is not None:
                    trace.append({"step": "spectral_features", "source": "db_cache"})
                return {
                    "algebraic_connectivity": fp.algebraic_connectivity,
                    "spectral_gap": fp.spectral_gap,
                    "signal_smoothness": fp.signal_smoothness,
                    "node_count": fp.node_count,
                    "edge_count": fp.edge_count,
                    "eigenvalues": fp.eigenvalues,
                }
        return {"error": "No spectral fingerprint available (Step 5c not run)"}

    @classmethod
    def _execute_temporal_drift(cls, concepts, country_code,
                                snapshot_date, trace):
        """TEMPORAL-DRIFT — spectral drift between snapshots."""
        from semantic_search.models import GraphSpectralDrift

        qs = GraphSpectralDrift.objects.filter(region=country_code)
        if snapshot_date:
            qs = qs.filter(snapshot_to__snapshot_date=snapshot_date)
        drift = qs.order_by('-created_at').first()
        if drift is None:
            return {"error": "No spectral drift available (need >=2 snapshots)"}
        if trace is not None:
            trace.append({
                "step": "drift_metrics",
                "snapshot_from": drift.snapshot_from.snapshot_date,
                "snapshot_to": drift.snapshot_to.snapshot_date,
            })
        return {
            "spectral_distance": drift.spectral_distance,
            "connectivity_delta": drift.connectivity_delta,
            "spectral_gap_delta": drift.spectral_gap_delta,
            "fiedler_drift": drift.fiedler_drift,
            "smoothness_delta": drift.smoothness_delta,
            "drift_magnitude": drift.drift_magnitude,
            "changepoint_detected": drift.changepoint_detected,
            "forecast_eigenvalues": drift.forecast_eigenvalues,
            "snapshot_from": drift.snapshot_from.snapshot_date,
            "snapshot_to": drift.snapshot_to.snapshot_date,
        }

    @classmethod
    def _execute_community_detect(cls, concepts, country_code,
                                  snapshot_date, trace):
        """COMMUNITY-DETECT — Louvain communities + optional class filter.

        Reads stored Louvain assignments from factor_spectral_node_metric
        via FactorResolutionService.community_summary (SQL GROUP BY).
        The on-the-fly NetworkX community detection path has been removed.
        """
        if country_code:
            table_result = cls._community_detect_via_tables(
                concepts, country_code, snapshot_date, trace,
            )
            if table_result is not None:
                return table_result
            if trace is not None:
                trace.append({"step": "factor_join",
                              "warning": "table path unavailable"})
        return {"error": "No community data available (Step 5c not run)"}

    @classmethod
    def _execute_event_diffusion(cls, concepts, country_code,
                                 snapshot_date, trace):
        """EVENT-DIFFUSION — heat kernel diffusion from a source entity.

        SQL-only diffusion via stored eigen-loadings (pgvector <#> query).
        The on-the-fly NetworkX/SciPy heat kernel path has been removed.
        """
        # SUB_COND: source LOCATION → geocode to an OSM entity
        source_concept = cls._get_concept(concepts, "LOCATION")
        if not source_concept or not source_concept.get("text"):
            return {"error": "Event diffusion requires a source location"}

        snap = cls._get_snapshot_id(snapshot_date)
        source_entity = EntityGeocoder.geocode(
            source_concept["text"], country_code, snap
        )
        if not source_entity or not source_entity.get("osm_id"):
            return {"error": f"Could not geocode source: {source_concept['text']}"}
        if trace is not None:
            trace.append({
                "step": "geocode_source",
                "input": source_concept["text"],
                "osm_id": source_entity.get("osm_id"),
            })

        # COND: time window (AMOUNT) → diffusion times
        amount = cls._get_concept(concepts, "AMOUNT")
        t_values = [1.0, 5.0, 10.0]
        if amount and amount.get("text"):
            parsed = cls._parse_radius(amount["text"])
            if parsed:
                # Interpret the radius as a diffusion time scale
                t_values = [float(parsed) / 10.0, float(parsed) / 2.0, float(parsed)]

        # Factor-table path: SQL-only diffusion via stored eigen-loadings.
        if country_code:
            table_result = cls._event_diffusion_via_tables(
                source_entity["osm_id"], t_values, snap, country_code, trace,
            )
            if table_result is not None:
                return table_result
            if trace is not None:
                trace.append({"step": "factor_join",
                              "warning": "table path unavailable"})

        return {"error": "No diffusion data available (Step 5c not run)"}

    # ── Geo + USLP enrichment ───────────────────────────────────────────────

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

    @staticmethod
    def _synthesize_answer(template: str, concepts: list,
                           results: list, trace: list,
                           skip_llm: bool = False) -> str:
        """Generate a grounded natural-language answer.

        LLM-first, template fallback: when the platform LLM (Ollama, see
        core/services/llm_service.py) is available, it composes a grounded
        answer from the results + trace ([SPATIAL_AGENT:§F.2] a = L_gen(q, Σ_M,
        F)). Any failure — model down, timeout, empty LLM output — falls back
        to the deterministic per-template formatter below, so answers never
        break because the local model is unavailable.

        ``skip_llm=True`` bypasses the LLM pass — used when the enrichment
        research loop (QueryEnrichmentService) will run anyway, so the answer
        is not rewritten twice (one fewer LLM call per request).
        """
        if not skip_llm:
            llm_answer = QueryExecutorService._llm_synthesize_answer(
                template, concepts, results, trace,
            )
            if llm_answer:
                return llm_answer

        if not results:
            return "No results found."
        if isinstance(results, dict) and "error" in results:
            return results["error"]

        count = len(results) if isinstance(results, list) else 1

        if template == "FILTER-AGGREGATE-MEASURE (#1)":
            radius = QueryExecutorService._get_concept(concepts, "AMOUNT")
            radius_text = radius["text"] if radius else "the specified radius"
            return f"Found {count} entities within {radius_text}."

        if template == "GEOCODE-BATCH-COMPARE (#4)":
            if results and isinstance(results, list):
                top = results[0]
                dist = top.get("distance_m")
                if dist is not None:
                    return f"Nearest: {top.get('name', 'unknown')} ({dist:.0f}m away)."
                return f"Nearest: {top.get('name', 'unknown')}."

        if template == "PLACE-ATTRIBUTE-QUERY (#8)":
            if results and isinstance(results, list):
                count = len(results)
                names = [r.get("name", "N/A") for r in results[:3]]
                if count == 1:
                    return f"Found: {names[0]}."
                return f"Found {count} places: {', '.join(names)}" + \
                       ("..." if count > 3 else ".")

        if template == "LOCATION-BEARING-CLASSIFY (#5)":
            if results and isinstance(results, list) and "direction" in results[0]:
                r = results[0]
                return (f"Direction: {r['direction']} "
                        f"({r['bearing_degrees']:.0f}°) "
                        f"from {r['from']} to {r['to']}.")

        if template == "OBJECT-FIELD-MEASURE (#2)":
            if results and isinstance(results, list) and "distance_km" in results[0]:
                r = results[0]
                return (f"Distance: {r['distance_km']:.2f} km "
                        f"({r['distance_m']:.0f} m) "
                        f"from {r['from']} to {r['to']}.")

        if template == "SPECTRAL-ANALYSIS (#11)":
            if isinstance(results, dict) and "algebraic_connectivity" in results:
                return (
                    f"Algebraic connectivity λ₂ = "
                    f"{results['algebraic_connectivity']:.6f}, "
                    f"spectral gap = {results['spectral_gap']:.6f}, "
                    f"signal smoothness = {results.get('signal_smoothness', 0.0):.4f}."
                )

        if template == "TEMPORAL-DRIFT (#12)":
            if isinstance(results, dict) and "spectral_distance" in results:
                return (
                    f"Spectral drift = {results['spectral_distance']:.4f} "
                    f"({results.get('drift_magnitude', 'unknown')}), "
                    f"Δλ₂ = {results['connectivity_delta']:.4f}, "
                    f"Fiedler drift = {results['fiedler_drift']:.4f}."
                )

        if template == "COMMUNITY-DETECT (#13)":
            if isinstance(results, dict) and "community_count" in results:
                return (
                    f"Detected {results['community_count']} communities "
                    f"(modularity Q = {results.get('modularity', 0.0):.4f})."
                )

        if template == "EVENT-DIFFUSION (#14)":
            if isinstance(results, dict) and "affected" in results:
                t_keys = sorted(results["affected"].keys())
                counts = [len(results["affected"][t]) for t in t_keys]
                return (
                    f"Event diffusion from osm_id {results.get('source_osm_id')}: "
                    f"{', '.join(f't={t}→{c} entities' for t, c in zip(t_keys, counts))}."
                )

        return f"Found {count} results."

    @staticmethod
    def _llm_synthesize_answer(template: str, concepts: list,
                               results: list, trace: list) -> Optional[str]:
        """LLM-grounded answer synthesis — fail-soft, returns None on any error.

        Only fires when there is actual result content to ground on (an empty
        or error result adds nothing an LLM can say). The prompt receives a
        compact, fully-serialized context so the LLM cannot hallucinate
        counts or distances beyond what the executor produced.
        """
        if not results or (isinstance(results, dict) and "error" in results):
            return None
        try:
            from core.services.llm_service import LLMService
            llm = LLMService.get_instance()
            if not llm.is_available():
                return None

            concept_lines = []
            for c in concepts:
                if c.get("text"):
                    concept_lines.append(f"{c.get('type')}: {c.get('text')}")
                elif c.get("type"):
                    concept_lines.append(f"{c.get('type')}: (none)")

            result_lines = []
            if isinstance(results, list):
                for r in results[:8]:
                    name = r.get("name") or (r.get("tags") or {}).get("name") or "N/A"
                    bits = [str(name)]
                    if r.get("distance_m") is not None:
                        bits.append(f"{float(r['distance_m']):.0f}m")
                    if r.get("distance_km") is not None:
                        bits.append(f"{float(r['distance_km']):.2f}km")
                    if r.get("score") is not None:
                        bits.append(f"score={r['score']:.3f}")
                    if r.get("wkg_class"):
                        bits.append(str(r["wkg_class"]))
                    result_lines.append(" | ".join(bits))
            elif isinstance(results, dict):
                for k, v in list(results.items())[:8]:
                    result_lines.append(f"{k}: {v}")

            trace_steps = [t.get("step") for t in (trace or []) if t.get("step")]

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a geospatial reasoning assistant grounded in "
                        "WorldKG pipeline output. Answer concisely in 1-3 "
                        "sentences using ONLY the provided template, concepts, "
                        "results, and execution trace. Never invent entities, "
                        "distances, counts, or scores. If the results are "
                        "insufficient, say so."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Template: {template}\n"
                        f"Concepts: {', '.join(concept_lines) or '(none)'}\n"
                        f"Results:\n" + ("\n".join(result_lines) or "(none)") +
                        f"\nExecution trace: {', '.join(trace_steps) or '(none)'}\n"
                        "Write the natural-language answer."
                    ),
                },
            ]
            answer = llm.chat(messages, temperature=0.2, max_tokens=200)
            answer = (answer or "").strip()
            return answer or None
        except Exception as exc:  # noqa: BLE001 — answer synthesis must never break execute
            logger.warning("LLM answer synthesis failed; using template formatter: %s", exc)
            return None
