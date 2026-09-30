"""QueryExecutorService — core dispatch, helpers, factor-table paths.

Extracted from query_executor_service.py (monolith split, Phase 5).
"""
import logging
from typing import Optional
from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id
import re
import time

from semantic_search.services.query_executor_service.graph_executors import GraphExecutorsMixin
from semantic_search.services.query_executor_service.geo_uslp import GeoUslpMixin
from semantic_search.services.query_executor_service.spatial_search import SpatialSearchMixin
from semantic_search.services.query_executor_service.synthesis import SynthesisMixin
from semantic_search.services.query_executor_service.template_executors import TemplateExecutorsMixin

logger = logging.getLogger(__name__)

class QueryExecutorService(
    TemplateExecutorsMixin,
    SpatialSearchMixin,
    GraphExecutorsMixin,
    GeoUslpMixin,
    SynthesisMixin,
):
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
                event_callback=None, skip_enrichment: bool = False) -> dict:
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
            skip_enrichment: Skip the deterministic context fetch + LLM
                synthesis block. The research orchestrator uses this: its
                loop reasons over deterministic primary answers and does its
                own summary synthesis at the end (one LLM in the loop).

        Returns:
            {template, results, answer, trace, latency_ms}
        """
        start = time.monotonic()
        template = parsed["template"]
        concepts = parsed["concepts"]

        # For multi-entity templates, supplement concepts with full entity
        # extraction. LOCATION-BEARING-CLASSIFY (#5) is intentionally NOT in
        # this set: its cone path needs ONE anchor + a direction, and the
        # regex extraction over-splits names ("the Spire of Dublin" →
        # ["Spire", "Dublin"]) which hijacks cone questions into the
        # two-location bearing path. The parser's own LOCATION concepts
        # already cover the 5a bearing case ("Which direction is X from Y?").
        if question and template in (
            "OBJECT-FIELD-MEASURE (#2)",
            "GEOCODE-BATCH-COMPARE (#4)",
        ):
            from semantic_search.services.query_parser_service import QueryParserService
            all_entities = QueryParserService.extract_all_entities(question)
            existing_locations = cls._get_concepts_by_type(concepts, "LOCATION")
            # Filter out interrogative / non-entity tokens the regex fallback can emit.
            stop_words = getattr(QueryParserService, "_QUESTION_WORDS", set())
            clean_entities = [e for e in all_entities if e not in stop_words]
            if len(clean_entities) >= 2 and len(existing_locations) < 2:
                # Replace/augment LOCATION concepts with full extraction
                concepts = [c for c in concepts if c["type"] != "LOCATION"]
                for entity_name in clean_entities:
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
            import inspect
            sig = inspect.signature(executor)
            if "question" in sig.parameters:
                results = executor(concepts, country_code, snapshot_date, trace, question=question)
            else:
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

            # Deterministic factor-join answer (the raw, non-AI result) —
            # emit it immediately so the client renders it before the LLM
            # synthesis finishes; the enriched answer_delta stream replaces
            # it when ready.
            if event_callback and question:
                event_callback({"event": "answer", "answer": answer})

            # Enrichment (direct path): fetch deterministic entity context
            # (USLP links + communities + class distribution — no LLM
            # selection step), then a single grounded synthesis call.
            # Fail-soft — the templated answer is kept on any failure (see
            # entity_context_service.py / query_enrichment_service.py).
            enrichment = None
            # Enrichment requires deterministic entity context — the context
            # sources (uslp/communities/classes) are all keyed by osm_id.
            # With none, the LLM synthesizes from empty context and
            # contradicts the deterministic answer (observed twice:
            # "no entities to the west" on an error result, and "distance
            # not provided" vs "Distance: 0.44 km" on a distance result).
            error_result = (
                isinstance(results, list)
                and results
                and isinstance(results[0], dict)
                and bool(results[0].get("error"))
            )
            has_entity_context = (
                isinstance(results, list)
                and any(
                    isinstance(r, dict) and r.get("osm_id")
                    for r in results
                )
            )
            if (
                question
                and not skip_enrichment
                and isinstance(results, list)
                and not error_result
                and has_entity_context
            ):
                from semantic_search.services.entity_context_service import (
                    EntityContextService,
                )
                from semantic_search.services.query_enrichment_service import (
                    QueryEnrichmentService,
                )

                # Deterministic context — no LLM tool selection.
                osm_ids = [r["osm_id"] for r in results if r.get("osm_id")]
                # Resolve the effective country from the result entities when
                # the request has none: the class-distribution context query
                # otherwise Appends over EVERY country partition of the
                # snapshot (measured 3.4-12.4s on 2025_12_31 vs ~295ms
                # pruned to one leaf — rules §6.6 partitioning).
                effective_country = country_code
                if not effective_country:
                    countries = {
                        r.get("country_code") for r in results
                        if r.get("country_code")
                    }
                    if len(countries) == 1:
                        effective_country = countries.pop()
                context = EntityContextService.get_context(
                    osm_ids, effective_country, snapshot_date,
                    template=template, trace=trace,
                )
                if event_callback:
                    event_callback({"event": "context", "context": context})

                # Single LLM call — synthesis with context.
                enrichment = QueryEnrichmentService.synthesize(
                    question, template, concepts, results, context,
                    effective_country, snapshot_date,
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
            tags__contains={"amenity": amenity_type},
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        ids = list(qs.values_list('osm_id', flat=True)[:cap])
        if ids:
            return ids, "exact_tag"
        
        # DB-driven resolution (rule 6.2 — hardening Phase 5): the
        # factor_amenity_class_mapping table, populated by
        # compute_amenity_class_mappings (data + ontology tiers).
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )

        wkg_class = FactorResolutionService().amenity_class(amenity_type)
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

        # Fuzzy correction tier (Kuhn's Template correction layer): a
        # misspelled phrase fails both exact tiers above. Correct against
        # the snapshot's real tag vocabulary (pg_trgm + fuzzystrmatch) and
        # retry the exact-tag tier with the canonical tag.
        from semantic_search.services.query_correction_service import (
            QueryCorrectionService,
        )
        corrected = QueryCorrectionService.correct_amenity(
            amenity_type, country_code=country_code, snapshot_id=snapshot_id,
        )
        if corrected and corrected[0] != amenity_type:
            qs = OsmEntity.objects.using("vectors").filter(
                snapshot_id=snapshot_id,
                tags__contains={"amenity": corrected[0]},
            )
            if country_code:
                qs = qs.filter(country_code=country_code.upper())
            ids = list(qs.values_list('osm_id', flat=True)[:cap])
            if ids:
                return ids, "fuzzy_correction"
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

        Returns a result dict compatible with the graph path.  Modularity
        and community_count come from the persisted GraphSpectralFingerprint
        row (0c, 2026-09-11); modularity stays None when no fingerprint row
        exists for the country (subdivision countries report None until the
        region-resolution prerequisite lands).  Returns None when no factor
        rows exist (caller falls back).
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

        # 0c: persisted modularity from the country-level fingerprint (default
        # DB). Subdivision countries without a country-level row keep None.
        modularity = None
        from osmsnapshot.models import Snapshot
        from semantic_search.models import GraphSpectralFingerprint

        snapshot = (
            Snapshot.objects.using("default")
            .filter(country_code__iexact=country_code, snapshot_date=snap)
            .order_by("-created_at")
            .first()
        )
        if snapshot is not None:
            fp = (
                GraphSpectralFingerprint.objects
                .filter(region__iexact=country_code, snapshot=snapshot)
                .order_by("-created_at")
                .first()
            )
            if fp is not None:
                modularity = fp.modularity

        result = {
            "community_count": summary["community_count"],
            "modularity": modularity,
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
            "country_code": entity.country_code,
        }

    # ── Template 1: GEOCODE-BATCH-COMPARE (#4) ──────────────────────────────
    # "Which X is nearest to Y?" / "Which is closer to Y: X1, X2, or X3?"
    #
    # Graph-grounded execution ([SPATIAL_AGENT:§3.5]):
    #   1. SUPPORT: geocode the anchor → find its node in the k-NN graph
    #   2. SUB_COND: Dijkstra single-source shortest path from anchor
    #   3. Filter by amenity type, rank by graph distance (network proximity)
    #   4. MEASURE: report top-k nearest by graph distance
