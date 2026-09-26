"""TemplateExecutorsMixin — extracted from query_executor_service.py (monolith split, Phase 5)."""

import logging
from semantic_search.services.entity_geocoder import EntityGeocoder
from django.db.models import FloatField
from typing import Optional
from worldkg_nca.models import OsmEntity
from django.contrib.gis.geos import Point
from django.db.models.expressions import RawSQL
import math
import re
import time

from semantic_search.services.query_executor_service._constants import (
    AMENITY_TAG_ALIASES,
    AMENITY_TO_WKGS,
    GENERIC_AMENITY_PHRASES,
    GENERIC_AMENITY_KEYS,
    DEFAULT_NEAR_RADIUS_M,
    DIRECTION_NEAREST_RADIUS_M,
    PROXIMITY_QUESTION_RE,
)

logger = logging.getLogger(__name__)


class TemplateExecutorsMixin:
    """Mixin providing executor methods to QueryExecutorService."""

    @classmethod
    def _execute_geocode_batch_compare(cls, concepts, country_code,
                                       snapshot_date, trace, question=None):
        amenity = cls._get_concept(concepts, "OBJECT")
        locations = cls._get_concepts_by_type(concepts, "LOCATION")
        amenity_text = amenity["text"] if amenity else None

        # ── Pattern 2: "Which/What is closer to Y: X1 or X2?" ──
        # The parser can emit a bogus OBJECT for this phrasing (e.g.
        # "is closer" from "What is closer to X: A or B?"). Treat a
        # non-amenity OBJECT as noise so the compare branch still fires
        # when multi-entity extraction produced 3+ named locations.
        if len(locations) >= 3 and (not amenity_text
                                    or cls._resolve_amenity_tag(
                                        amenity_text, country_code, snapshot_date,
                                    ) is None):
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

        # 2. PostGIS Distance ordering (graph path removed — factor tables
        #    don't cover this template; PostGIS is the primary path)
        if anchor_coords and anchor_coords.get("lat"):
            anchor_point = Point(
                anchor_coords["lon"], anchor_coords["lat"], srid=4326
            )
            # Open-ended "X near Y" phrasings misrouted here by the parser
            # get a default spatial bound — otherwise the unbounded pool
            # (e.g. 535K shop-keyed entities on IE) is fully sorted by
            # distance (~9s) and the "nearby" answer isn't spatially honest.
            spatial_radius = None
            if question and PROXIMITY_QUESTION_RE.search(question):
                spatial_radius = DEFAULT_NEAR_RADIUS_M
                trace.append({
                    "step": "default_radius",
                    "radius_m": DEFAULT_NEAR_RADIUS_M,
                    "reason": "open-ended proximity question, no explicit radius",
                })
            entities = cls._search_by_amenity_spatial(
                amenity_text, country_code, snapshot_date,
                anchor_point=anchor_point,
                radius_m=spatial_radius, top_k=50, trace=trace,
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

        if not anchor or not anchor.get("osm_id") or not anchor.get("lat"):
            trace.append({"step": "compare_closer",
                          "error": "could not geocode anchor"})
            return []

        anchor_point = Point(anchor["lon"], anchor["lat"], srid=4326)
        results = []
        # Candidate geocodes prune to the anchor's country leaf when the
        # request has none: an unpruned name search Appends over EVERY
        # country partition of the snapshot (measured ~1-3s cold per
        # geocode on the 2025_12_31 snapshot vs ~50ms on one leaf).
        candidate_country = country_code or anchor.get("country_code")
        # Enriched candidates: input query + resolved entity (id, name,
        # distance) so the trace can link each candidate to its OSM entity
        # regardless of name matching. One entry per candidate, in input
        # order — failed geocodes stay present with null entity fields.
        enriched_candidates = []
        for name in candidate_names:
            entity = EntityGeocoder.geocode(
                name, candidate_country, snapshot_date,
            )
            if entity is None and candidate_country != country_code:
                # Cross-country candidate (e.g. border compare questions
                # "Windsor or Detroit") — retry unpruned. Rare, so the
                # extra Append is the exception, not the rule.
                entity = EntityGeocoder.geocode(
                    name, country_code, snapshot_date,
                )
            if entity and entity.get("lat"):
                dist_m = cls._haversine_m(
                    anchor["lat"], anchor["lon"],
                    entity["lat"], entity["lon"],
                )
                entity["distance_m"] = round(dist_m, 1)
                results.append(entity)
            else:
                entity = None
            enriched_candidates.append({
                "query": name,
                "name": entity.get("name") if entity else None,
                "osm_id": entity.get("osm_id") if entity else None,
                "osm_type": entity.get("osm_type") if entity else None,
                "distance_m": entity.get("distance_m") if entity else None,
            })

        results.sort(key=lambda x: x.get("distance_m") or float("inf"))
        trace.append({"step": "compare_closer",
                      "candidates": enriched_candidates,
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
                                          snapshot_date, trace, question=None):
        amenity = cls._get_concept(concepts, "OBJECT")
        radius_concept = cls._get_concept(concepts, "AMOUNT")
        anchor = cls._get_concept(concepts, "LOCATION")

        radius_m = cls._parse_radius(radius_concept["text"] if radius_concept else None)
        amenity_text = amenity["text"] if amenity else None

        # 1. SUPPORT: geocode the anchor — unless it is a generic amenity
        #    category ("police stations", "schools"), which the
        #    multi-anchor path resolves instead. Geocoding a category as a
        #    place name burns a full name-scan miss (~10-20s on LK).
        anchor_coords = None
        anchor_is_category = False
        if anchor and anchor["text"]:
            anchor_is_category = (
                cls._resolve_amenity_tag(
                    anchor["text"], country_code, snapshot_date,
                ) is not None
            )
            if not anchor_is_category:
                anchor_coords = EntityGeocoder.geocode(
                    anchor["text"], country_code, snapshot_date
                )
                trace.append({"step": "geocode", "input": anchor["text"],
                              "output": anchor_coords})

        # Default spatial bound for open-ended proximity questions
        # ("What X are near/around Y?") with no explicit AMOUNT. Bounds
        # the candidate pool AND makes the answer spatially honest — the
        # old no-radius path returned country-wide pools. Only fires when
        # the question itself signals proximity, so "cafes in Dublin"
        # keeps its unfiltered semantics. Explicit radii always win.
        if radius_m is None and anchor and anchor["text"] and question \
                and PROXIMITY_QUESTION_RE.search(question):
            radius_m = DEFAULT_NEAR_RADIUS_M
            trace.append({
                "step": "default_radius",
                "radius_m": DEFAULT_NEAR_RADIUS_M,
                "reason": "open-ended proximity question, no explicit radius",
            })

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
        anchor_amenity = cls._resolve_amenity_tag(
            anchor_text, country_code, snapshot_date,
        )
        if anchor_amenity is None:
            return None

        trace.append({
            "step": "multi_anchor_resolve",
            "input": anchor_text,
            "resolved_amenity": anchor_amenity,
        })

        # Fast path: when the target amenity is an exact OSM tag, resolve
        # "X within radius of ANY anchor" in ONE spatial self-join. The
        # previous per-anchor loop issued an ST_DWithin query PER anchor —
        # LK has 490 police stations → ~20s; the join is one query.
        amenity_tag = cls._resolve_amenity_tag(
            amenity_text, country_code, snapshot_date,
        )
        if amenity_tag:
            join_results = cls._multi_anchor_spatial_join(
                amenity_tag, anchor_amenity, country_code, snapshot_date,
                radius_m, top_k, trace,
            )
            if join_results is not None:
                return join_results
            # Join failed — fall through to the per-anchor loop.

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
    def _multi_anchor_spatial_join(cls, amenity_tag, anchor_tag, country_code,
                                   snapshot_date, radius_m, top_k, trace):
        """One spatial self-join: candidates within ``radius_m`` of ANY anchor.

        ``semantic_search_osmentity`` is partitioned by (snapshot_id,
        country_code), so both sides of the self-join land on the same
        leaf; ST_DWithin uses the GiST geography index on the anchor side
        (idx_osmentity_geom_geog).  ``distance_m`` = distance to the
        NEAREST anchor (MIN over the join), matching the per-anchor loop
        semantics.  Returns result dicts compatible with the loop, or
        None when the query fails (caller falls back to the loop).
        """
        sql = """
            WITH anchors AS (
                SELECT ST_Collect(geom) AS g
                FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags @> %s::jsonb AND geom IS NOT NULL
            )
            SELECT c.osm_id, c.osm_type, c.tags, c.wkg_class,
                   ST_Y(c.geom) AS lat, ST_X(c.geom) AS lon,
                   ST_Distance(c.geom::geography, a.g::geography) AS dist_m
            FROM semantic_search_osmentity c
            CROSS JOIN anchors a
            WHERE c.snapshot_id = %s AND c.country_code = %s
              AND c.tags @> %s::jsonb AND c.geom IS NOT NULL
              AND ST_DWithin(c.geom::geography, a.g::geography, %s)
            ORDER BY dist_m
            LIMIT %s
        """
        import json as _json

        from django.db import connections

        snapshot_id = snapshot_date or cls._get_snapshot_id(None)

        params = [
            snapshot_id,
            country_code.upper(),
            _json.dumps({"amenity": anchor_tag}),
            snapshot_id,
            country_code.upper(),
            _json.dumps({"amenity": amenity_tag}),
            float(radius_m),
            top_k,
        ]
        try:
            with connections["vectors"].cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001 — fall back to the per-anchor loop
            logger.warning("Multi-anchor spatial join failed (%s); using per-anchor loop", exc)
            return None

        results = []
        for row in rows:
            # psycopg2 returns jsonb as a JSON string on raw cursors.
            tags = row["tags"] or {}
            if isinstance(tags, str):
                try:
                    tags = _json.loads(tags or "{}")
                except ValueError:
                    tags = {}
            results.append({
                "osm_id": row["osm_id"],
                "osm_type": row["osm_type"],
                "name": tags.get("name", ""),
                "tags": tags,
                "lat": row["lat"],
                "lon": row["lon"],
                "wkg_class": row["wkg_class"],
                "distance_m": round(float(row["dist_m"]), 1),
            })
        trace.append({
            "step": "multi_anchor_search",
            "input": amenity_tag,
            "anchor_category": anchor_tag,
            "radius_m": radius_m,
            "output_count": len(results),
        })
        return results
    @classmethod
    def _resolve_amenity_tag(cls, text, country_code=None, snapshot_date=None):
        """Resolve a free-text phrase to an OSM amenity tag value.

        Returns the canonical amenity tag value (e.g. "police", "school")
        if the text maps to a known amenity, or ``None`` if it doesn't
        look like an amenity category (so the caller can treat it as a
        place name).

        Order: canonical alias map (no DB) → ontology map (no DB) →
        country-scoped DB existence check. The DB check is scoped by
        ``country_code`` when given — the previous unscoped exists()
        scanned EVERY partition of the snapshot (measured ~52s on LK
        after multiple countries were processed).
        """
        if not text:
            return None
        key = text.lower().strip().rstrip("s")  # singularise
        # Canonical alias map first — no DB hit ("police stations" →
        # "police_station" → the standard OSM tag value "police").
        alias_key = key.replace(" ", "_")
        if alias_key in AMENITY_TAG_ALIASES:
            return AMENITY_TAG_ALIASES[alias_key]
        # Ontology class map — the singularized key IS the tag value.
        if key in AMENITY_TO_WKGS:
            return key
        # Direct amenity tag match — bounded by country when available.
        # `tags__contains` renders the `@>` operator (GIN-accelerated);
        # `tags__amenity=value` renders `->>` equality and full-scans.
        from worldkg_nca.models import OsmEntity
        snapshot_id = snapshot_date or cls._get_snapshot_id(None)
        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
            tags__contains={"amenity": key},
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        if qs.exists():
            return key

        # Fuzzy correction tier (Kuhn's Template correction layer): the
        # phrase is not a known amenity tag. Correct misspellings against
        # the snapshot's real tag vocabulary (pg_trgm + fuzzystrmatch) so
        # the caller receives a canonical tag instead of None — and the
        # FastText tier, when reached, embeds the corrected phrase rather
        # than a garbage token.
        from semantic_search.services.query_correction_service import (
            QueryCorrectionService,
        )
        corrected = QueryCorrectionService.correct_amenity(
            text, country_code=country_code, snapshot_id=snapshot_id,
        )
        if corrected:
            return corrected[0]
        return None
    @staticmethod
    def _generic_amenity_keys(text) -> Optional[tuple]:
        """Normalize a phrase to generic-amenity OSM keys, or None.

        "amenities are" / "amenities" / "places" → the OSM keys that
        assert an amenity type. Strips trailing verb/copula fragments
        (" are", " is", " near", ...) that the parser's open-vocabulary
        OBJECT extraction tends to include. Returns None for specific
        categories (handled by the tag/ontology/FastText tiers) so the
        caller falls through.
        """
        if not text:
            return None
        phrase = text.lower().strip()
        for frag in (" are", " is", " near", " around", " close", " nearby",
                     " within", " in", " of", " at"):
            if phrase.endswith(frag):
                phrase = phrase[:-len(frag)].rstrip()
                break
        if phrase in GENERIC_AMENITY_PHRASES:
            return GENERIC_AMENITY_KEYS
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
                                            snapshot_date, trace, question=None):
        from semantic_search.services.query_parser_service import QueryParserService

        locations = cls._get_concepts_by_type(concepts, "LOCATION")

        # Determine if 5a (bearing pair) or 5b (cone search)
        cardinal = None
        if len(locations) < 2:
            # Parse cardinal direction from question or concept texts
            if question:
                m = re.search(
                    r"\b(north|northeast|east|southeast|south|southwest|west|northwest)\b",
                    question, re.IGNORECASE
                )
                if m:
                    cardinal = m.group(1).lower()
            if not cardinal:
                # Try finding it in any concept text (e.g. if question is not passed)
                for c in concepts:
                    if c.get("text"):
                        m = re.search(
                            r"\b(north|northeast|east|southeast|south|southwest|west|northwest)\b",
                            c["text"], re.IGNORECASE
                        )
                        if m:
                            cardinal = m.group(1).lower()
                            break

        if len(locations) >= 2:
            # ──── 5a: Bearing between two locations ────
            a = EntityGeocoder.geocode(locations[0]["text"], country_code, snapshot_date)
            b = EntityGeocoder.geocode(locations[1]["text"], country_code, snapshot_date)
            trace.append({"step": "batch_geocode",
                          "inputs": [locations[0]["text"], locations[1]["text"]],
                          "outputs": [a, b]})

            if not a or not b or not a.get("osm_id") or not b.get("osm_id") \
                    or not a.get("lat") or not b.get("lat"):
                if trace and trace[-1].get("step") == "batch_geocode":
                    trace[-1]["error"] = "Could not geocode one or both entities"
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

        elif len(locations) == 1 and cardinal:
            # ──── 5b: Cone search ────
            anchor_name = locations[0]["text"]
            anchor = EntityGeocoder.geocode(anchor_name, country_code, snapshot_date)
            trace.append({"step": "geocode", "input": anchor_name, "output": anchor})

            if not anchor or not anchor.get("osm_id") or not anchor.get("lat"):
                # Fail fast: an anchor needs a usable OSM entity (id +
                # coordinates). Flag the geocode step itself so the trace
                # UI shows the error badge (the error would otherwise only
                # live in the result dict and look like a clean step).
                if trace and trace[-1].get("step") == "geocode":
                    trace[-1]["error"] = f"Could not geocode anchor: {anchor_name}"
                return [{"error": f"Could not geocode anchor: {anchor_name}"}]

            # Map cardinal to angle
            BEARING_BY_CARDINAL = {
                "north": 0, "northeast": 45, "east": 90, "southeast": 135,
                "south": 180, "southwest": 225, "west": 270, "northwest": 315
            }
            # Also support abbreviations
            abbrev_map = {
                "n": "north", "ne": "northeast", "e": "east", "se": "southeast",
                "s": "south", "sw": "southwest", "w": "west", "nw": "northwest"
            }
            norm_cardinal = cardinal.lower()
            if norm_cardinal in abbrev_map:
                norm_cardinal = abbrev_map[norm_cardinal]
            
            center_bearing = BEARING_BY_CARDINAL[norm_cardinal]

            # Determine amenity type if present
            object_concept = cls._get_concept(concepts, "OBJECT")
            amenity_type = None
            if object_concept and object_concept.get("text"):
                amenity_text = object_concept["text"].strip().lower()
                # If the extracted OBJECT text is the cardinal itself, it's not a real amenity
                if amenity_text != norm_cardinal and amenity_text not in abbrev_map:
                    amenity_type = object_concept["text"]

            # Fetch candidates within the direction cone radius. Default
            # 10km — bounds the candidate pool for open-ended direction
            # questions (was 20km → 60+ candidates per cone).
            radius_m = DIRECTION_NEAREST_RADIUS_M
            anchor_point = Point(anchor["lon"], anchor["lat"], srid=4326)

            if amenity_type:
                candidates = cls._search_by_amenity_spatial(
                    amenity_type, country_code, snapshot_date,
                    anchor_point=anchor_point,
                    radius_m=radius_m, top_k=200, trace=trace,
                )
            else:
                snapshot_id = cls._get_snapshot_id(snapshot_date)
                qs = OsmEntity.objects.using("vectors").filter(
                    snapshot_id=snapshot_id,
                    tags__name__isnull=False,
                ).exclude(geom__isnull=True)
                if country_code:
                    qs = qs.filter(country_code=country_code.upper())
                
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
                ).order_by("distance_m")[:200]
                
                try:
                    entities = list(qs)
                except Exception as exc:
                    logger.warning("Spatial cone search entities query failed: %s", exc)
                    entities = []
                candidates = [cls._entity_to_result(e) for e in entities]

            # Calculate bearing & filter by ±45° cone
            def get_bearing(lat1, lon1, lat2, lon2):
                phi1, lam1 = math.radians(lat1), math.radians(lon1)
                phi2, lam2 = math.radians(lat2), math.radians(lon2)
                dlam = lam2 - lam1
                y = math.sin(dlam) * math.cos(phi2)
                x = (math.cos(phi1) * math.sin(phi2) -
                     math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
                return (math.degrees(math.atan2(y, x)) + 360) % 360

            def angular_diff(a, b):
                diff = abs(a - b) % 360
                return diff if diff <= 180 else 360 - diff

            valid_candidates = []
            for c in candidates:
                # Exclude anchor itself
                if c.get("osm_id") == anchor.get("osm_id") and c.get("osm_type") == anchor.get("osm_type"):
                    continue
                c_lat = c.get("lat")
                c_lon = c.get("lon")
                if c_lat is None or c_lon is None:
                    continue
                
                theta = get_bearing(anchor["lat"], anchor["lon"], c_lat, c_lon)
                if angular_diff(theta, center_bearing) <= 45:
                    c_copy = dict(c)
                    c_copy["bearing_degrees"] = round(theta, 1)
                    c_copy["direction"] = norm_cardinal
                    c_copy["anchor_name"] = anchor_name
                    c_copy["requested_amenity"] = amenity_type
                    # Ensure distance_m is present and rounded
                    if "distance_m" not in c_copy:
                        dist_m = cls._haversine_m(anchor["lat"], anchor["lon"], c_lat, c_lon)
                        c_copy["distance_m"] = round(dist_m, 1)
                    valid_candidates.append(c_copy)

            trace.append({
                "step": "cone_search",
                "direction": norm_cardinal,
                "radius_m": radius_m,
                "amenity": amenity_type,
                "output_count": len(valid_candidates),
            })

            if not valid_candidates:
                return []

            # Sort by distance
            valid_candidates.sort(key=lambda x: x.get("distance_m", float("inf")))
            
            trace.append({
                "step": "rank_by_distance",
                "top": valid_candidates[0].get("name"),
            })

            return valid_candidates[:1]

        else:
            if len(locations) == 0:
                trace.append({"step": "location_parse",
                              "error": "no LOCATION concept found"})
            elif len(locations) == 1:
                trace.append({"step": "direction_parse",
                              "error": "could not determine a cardinal direction "
                                       "(north/east/south/west)"})
            else:
                trace.append({"step": "batch_geocode",
                              "error": "need 2 LOCATION concepts, got %d" % len(locations)})
            return []

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

        if not a or not b or not a.get("osm_id") or not b.get("osm_id") \
                or not a.get("lat") or not b.get("lat"):
            if trace and trace[-1].get("step") == "batch_geocode":
                trace[-1]["error"] = "Could not geocode one or both entities"
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
