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

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.gis.db.models.functions import Distance as GisDistance
from django.db import connection

from semantic_search.services.entity_geocoder import EntityGeocoder
from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)


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
            }
        return cls._executors

    @classmethod
    def execute(cls, parsed: dict, country_code: str = None,
                snapshot_date: str = None, question: str = None) -> dict:
        """Execute a parsed query and return results + execution trace.

        Args:
            parsed: {template, concepts, roles, dag, confidence, validation}
                    from QueryParserService.parse()
            country_code: ISO 3166-1 alpha-2 code
            snapshot_date: Optional YYYY_MM_DD snapshot filter
            question: Original question text (used for multi-entity extraction
                      in templates that need 2+ entities)

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

            answer = cls._synthesize_answer(template, concepts, results, trace)
            latency_ms = (time.monotonic() - start) * 1000

            logger.info("mapqa_execute", extra={
                "template": template,
                "country_code": country_code,
                "result_count": len(results) if isinstance(results, list) else 1,
                "latency_ms": round(latency_ms, 1),
            })

            return {
                "template": template,
                "results": results,
                "answer": answer,
                "trace": trace,
                "latency_ms": round(latency_ms, 1),
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

    @staticmethod
    def _get_snapshot_id(snapshot_date: str = None) -> str:
        return snapshot_date or get_latest_snapshot_id()

    @staticmethod
    def _parse_radius(text: str) -> int:
        """Parse a radius string like '50m' or '100m' → meters (int)."""
        if not text:
            return None
        m = re.search(r"(\d+)", text)
        return int(m.group(1)) if m else None

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

    @classmethod
    def _execute_geocode_batch_compare(cls, concepts, country_code,
                                       snapshot_date, trace):
        amenity = cls._get_concept(concepts, "OBJECT")
        locations = cls._get_concepts_by_type(concepts, "LOCATION")

        # ── Pattern 2: "Which is closer to Y: X1 or X2?" ──
        # When we have 3+ LOCATION concepts (from multi-entity extraction),
        # the last one is the anchor, the rest are named candidates.
        if len(locations) >= 3 and not amenity:
            return cls._execute_compare_closer(
                locations, country_code, snapshot_date, trace
            )

        # ── Pattern 1: "Which X is nearest to Y?" ──
        anchor = locations[0] if locations else None

        # 1. SUPPORT: geocode the anchor
        anchor_coords = None
        if anchor and anchor["text"]:
            anchor_coords = EntityGeocoder.geocode(
                anchor["text"], country_code, snapshot_date
            )
            trace.append({"step": "geocode", "input": anchor["text"],
                          "output": anchor_coords})

        # 2. SUB_COND + MEASURE: search for amenity entities, ordered by distance
        #    Uses PostGIS Distance annotation (GiST index-backed) when anchor
        #    is available, falling back to unfiltered search otherwise.
        if anchor_coords and anchor_coords.get("lat"):
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            entities = cls._search_by_amenity_spatial(
                amenity["text"] if amenity else None,
                country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=None,  # no radius filter — just order by distance
                top_k=50, trace=trace,
            )
            if entities:
                trace.append({"step": "rank_by_distance",
                              "anchor": anchor_coords.get("name"),
                              "top": entities[0].get("name")})
            return entities

        # No anchor — return unfiltered amenity search
        entities = cls._search_by_amenity(
            amenity["text"] if amenity else None,
            country_code, snapshot_date, top_k=50, trace=trace,
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

    @classmethod
    def _execute_filter_aggregate_measure(cls, concepts, country_code,
                                          snapshot_date, trace):
        amenity = cls._get_concept(concepts, "OBJECT")
        radius_concept = cls._get_concept(concepts, "AMOUNT")
        anchor = cls._get_concept(concepts, "LOCATION")

        radius_m = cls._parse_radius(radius_concept["text"] if radius_concept else None)

        # 1. SUPPORT: geocode the anchor
        anchor_coords = None
        if anchor and anchor["text"]:
            anchor_coords = EntityGeocoder.geocode(
                anchor["text"], country_code, snapshot_date
            )
            trace.append({"step": "geocode", "input": anchor["text"],
                          "output": anchor_coords})

        # 2. SUB_COND + COND: search for amenity entities within radius
        #    Uses PostGIS ST_DWithin (GiST index-backed) when anchor + radius
        #    are available, falling back to unfiltered search otherwise.
        if radius_m and anchor_coords and anchor_coords.get("lat"):
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            entities = cls._search_by_amenity_spatial(
                amenity["text"] if amenity else None,
                country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=radius_m,
                top_k=200, trace=trace,
            )
            return entities

        # No anchor or radius — return unfiltered amenity search
        entities = cls._search_by_amenity(
            amenity["text"] if amenity else None,
            country_code, snapshot_date, top_k=200, trace=trace,
        )
        return entities

    # ── Template 3: PLACE-ATTRIBUTE-QUERY (#8) ──────────────────────────────
    # "What amenity is available at Union Station?"

    @classmethod
    def _execute_place_attribute_query(cls, concepts, country_code,
                                       snapshot_date, trace):
        entity_concept = cls._get_concept(concepts, "OBJECT")
        if not entity_concept:
            entity_concept = cls._get_concept(concepts, "LOCATION")

        entity_name = entity_concept["text"] if entity_concept else None
        if not entity_name:
            trace.append({"step": "place_search", "error": "no entity name"})
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__name__icontains=entity_name,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        entities = list(qs[:10])
        trace.append({"step": "place_search", "input": entity_name,
                      "output_count": len(entities)})

        results = [cls._entity_to_result(e) for e in entities]
        trace.append({"step": "place_details", "output_count": len(results)})
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

    @classmethod
    def _execute_object_field_measure(cls, concepts, country_code,
                                      snapshot_date, trace):
        locations = cls._get_concepts_by_type(concepts, "LOCATION")
        # If only one LOCATION concept, try multi-entity extraction from question
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

        # Haversine distance
        dist_km = cls._haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        dist_m = dist_km * 1000
        trace.append({"step": "haversine",
                      "output_km": round(dist_km, 3),
                      "output_m": round(dist_m, 1)})

        return [{"distance_km": round(dist_km, 3),
                 "distance_m": round(dist_m, 1),
                 "from": locations[0]["text"],
                 "to": locations[1]["text"],
                 "from_coords": {"lat": a["lat"], "lon": a["lon"]},
                 "to_coords": {"lat": b["lat"], "lon": b["lon"]}}]

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
        """
        try:
            from semantic_search.services.fasttext_service import FastTextEmbeddingService
            from django.db.models import FloatField
            from django.db.models.expressions import RawSQL
            query_embedding = FastTextEmbeddingService.calculate_text_embedding(amenity_type)
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

        if trace is not None:
            trace.append({"step": "place_search",
                          "input": amenity_type,
                          "match_type": "fasttext",
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
        Distance annotation for proximity ordering. Falls back to
        _search_by_amenity if the spatial query fails.

        Args:
            amenity_type: Amenity tag value to search for
            country_code: ISO 3166-1 alpha-2 code
            snapshot_date: Snapshot partition key
            anchor_point: GeoDjango Point (lon, lat, srid=4326)
            radius_m: Optional radius in meters for ST_DWithin filter
            top_k: Maximum results to return
            trace: Optional execution trace list
        """
        if not amenity_type:
            return []

        snapshot_id = cls._get_snapshot_id(snapshot_date)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__amenity=amenity_type,
        ).exclude(geom__isnull=True)

        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        # Apply ST_DWithin radius filter if specified
        if radius_m is not None:
            qs = qs.filter(geom__dwithin=(anchor_point, D(m=radius_m)))

        # Order by distance to anchor (PostGIS GiST index-backed)
        qs = qs.annotate(distance=GisDistance("geom", anchor_point)).order_by("distance")[:top_k]

        try:
            entities = list(qs)
        except Exception as exc:
            logger.warning("Spatial amenity query failed, falling back: %s", exc)
            return cls._search_by_amenity(
                amenity_type, country_code, snapshot_date, top_k, trace
            )

        results = []
        for e in entities:
            r = cls._entity_to_result(e)
            # Extract distance from annotation (in meters)
            if hasattr(e, "distance"):
                r["distance_m"] = round(e.distance.m, 1)
            results.append(r)

        if trace is not None:
            trace.append({
                "step": "place_search_spatial",
                "input": amenity_type,
                "radius_m": radius_m,
                "output_count": len(results),
            })
        return results

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

    # ── Answer synthesis ────────────────────────────────────────────────────

    @staticmethod
    def _synthesize_answer(template: str, concepts: list,
                           results: list, trace: list) -> str:
        """Generate a grounded natural-language answer.

        Template-driven formatter (not an LLM call) — the 5 templates have
        deterministic answer formats. Following [SPATIAL_AGENT:§F.2].
        """
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
                r = results[0]
                tags = r.get("tags", {})
                amenity = tags.get("amenity", "unknown")
                return f"Attributes: amenity={amenity}, name={r.get('name', 'N/A')}."

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

        return f"Found {count} results."
