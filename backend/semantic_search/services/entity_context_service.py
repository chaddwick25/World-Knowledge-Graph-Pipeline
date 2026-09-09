"""
entity_context_service.py — deterministic enrichment context fetcher.

Replaces the LLM research-tool selection loop on the direct query path
(see docs/plans/DIRECT_PATH_ENRICHMENT_PLAN.md): instead of asking the
LLM which search tool to run (nameSearch / structuredSearch — both return
entity-matching data, i.e. confirmation), fetch three different types of
context from existing DB tables:

  - USLP spatial links (relational)   — igea.SpatialTripletScore
  - Community structure (structural)  — factor_spectral_node_metric
  - Class distribution (semantic)     — OsmEntity.wkg_class

Three indexed SQL queries, no LLM, no model inference, no HTTP hop.
Per-template source selection (TEMPLATE_CONTEXT_MAP) mirrors the parser's
template decision — no LLM tool selection step. Fail-soft per source:
any query failure yields an empty value for that key, never an exception.
"""

import logging
from typing import Any, Dict, List

from django.db.models import Count

logger = logging.getLogger(__name__)

# Per-template context sources (plan §3). Unknown template → all three
# (safe default).
TEMPLATE_CONTEXT_MAP = {
    "FILTER-AGGREGATE-MEASURE (#1)": {"uslp", "communities", "classes"},
    "OBJECT-FIELD-MEASURE (#2)": {"uslp", "communities"},
    "GEOCODE-BATCH-COMPARE (#4)": {"uslp", "communities", "classes"},
    "LOCATION-BEARING-CLASSIFY (#5)": {"classes"},
    "PLACE-ATTRIBUTE-QUERY (#8)": {"uslp", "classes"},
    # Graph templates already carry spectral/community context from the
    # executor — only add what they lack.
    "SPECTRAL-ANALYSIS (#11)": {"communities"},
    "TEMPORAL-DRIFT (#12)": {"communities"},
    "COMMUNITY-DETECT (#13)": {"communities"},
    "EVENT-DIFFUSION (#14)": {"communities"},
}

ALL_SOURCES = frozenset({"uslp", "communities", "classes"})

# Cap the USLP link payload — 50 links inflated the synthesis prompt to
# ~1.5K tokens (fresh queries 50-115s vs ~7s pre-refactor). 10 links +
# the prompt-side relation histogram (see _format_context_for_prompt in
# query_enrichment_service.py) carry the same signal.
USLP_LINK_CAP = 10


class EntityContextService:
    """Deterministic context fetcher for enrichment synthesis."""

    @classmethod
    def get_context(cls, osm_ids, country_code, snapshot_date=None,
                    snapshot_id=None, template=None, trace=None) -> Dict[str, Any]:
        """Fetch relational, structural, and semantic context for entities.

        Args:
            osm_ids: list of int — OSM IDs from the executor results
            country_code: ISO 3166-1 alpha-2
            snapshot_date: YYYY_MM_DD — resolved to ``snapshot_id`` when
                ``snapshot_id`` is not given
            snapshot_id: optional pre-resolved partition key (YYYY_MM_DD);
                when omitted, resolved once from the TTL-cached
                ``get_latest_snapshot_id()`` (never per source — that
                lookup is the request-path hot path, rules §5.7)
            template: optional — controls which context sources are
                fetched (per-template tool selection, plan §3)
            trace: optional execution trace list (``entity_context`` step)

        Returns:
            {
                "uslp_links": [{head_osm_id, relation, tail_osm_id,
                                normalized_score}, ...],
                "communities": {osm_id: {community, fiedler, degree,
                                         component_size, subgraph_slug}, ...},
                "class_distribution": [{wkg_class, count}, ...],
            }
            Any source returning empty → that key is [] or {}.
        """
        if not osm_ids:
            return {"uslp_links": [], "communities": {},
                    "class_distribution": []}

        if snapshot_id is None:
            from worldkg_nca.snapshot_utils import get_latest_snapshot_id
            snapshot_id = snapshot_date or get_latest_snapshot_id()

        sources = TEMPLATE_CONTEXT_MAP.get(template, ALL_SOURCES)

        context = {
            "uslp_links": (
                cls._uslp_links(osm_ids, snapshot_id)
                if "uslp" in sources else []
            ),
            "communities": (
                cls._communities(osm_ids, snapshot_id, country_code)
                if "communities" in sources else {}
            ),
            "class_distribution": (
                cls._class_distribution(osm_ids, snapshot_id, country_code)
                if "classes" in sources else []
            ),
        }

        if trace is not None:
            trace.append({
                "step": "entity_context",
                "sources": sorted(sources),
                "uslp_links": len(context["uslp_links"]),
                "communities": len(context["communities"]),
                "class_distribution": len(context["class_distribution"]),
            })
        return context

    @staticmethod
    def _uslp_links(osm_ids, snapshot_id: str) -> List[dict]:
        """Predicted USLP links where a result entity is the head.

        Scoped by ``snapshot_id`` (SpatialTripletScore.snapshot_id is
        populated by Step 4 and matches OsmEntity.snapshot_id — rules
        §2.3) so shared snapshots never leak links across countries.
        """
        try:
            from igea.models import SpatialTripletScore
            return list(
                SpatialTripletScore.objects.using("vectors")
                .filter(
                    head_osm_id__in=osm_ids,
                    predicted=True,
                    snapshot_id=snapshot_id,
                )
                .values("head_osm_id", "relation", "tail_osm_id",
                        "normalized_score")
                .order_by("-normalized_score")[:USLP_LINK_CAP]
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft per source
            logger.warning("USLP context fetch failed: %s", exc)
            return []

    @staticmethod
    def _communities(osm_ids, snapshot_id: str, country_code: str) -> Dict[int, dict]:
        """Louvain/Fiedler structure from the factor tables (Step 5c).

        G4-gated: skips the join entirely when no factor row exists for
        the country/snapshot (``factor_spectral_node_metric`` is only
        populated when Step 5c/5d ran), so countries without spectral
        data don't pay for a join that returns nothing.
        """
        if not country_code:
            return {}
        try:
            from semantic_search.services.factor_resolution_service import (
                FactorResolutionService,
            )
            svc = FactorResolutionService()
            availability = svc.check_availability(
                osm_ids, snapshot_id, country_code,
            )
            if not availability or not any(availability.values()):
                return {}
            metrics = svc.resolve_metrics(osm_ids, snapshot_id, country_code)
            return {
                int(osm_id): {
                    "community": m.get("louvain_community"),
                    "fiedler": m.get("fiedler_component"),
                    "degree": m.get("degree"),
                    "component_size": m.get("component_size"),
                    "subgraph_slug": m.get("subgraph_slug"),
                }
                for osm_id, m in metrics.items()
            }
        except Exception as exc:  # noqa: BLE001 — fail-soft per source
            logger.warning("Community context fetch failed: %s", exc)
            return {}

    @staticmethod
    def _class_distribution(osm_ids, snapshot_id: str,
                            country_code: str) -> List[dict]:
        """Ontological class mix of the result entities (wkgs: classes).

        Entities IGEA did not align (``wkg_class`` null/blank) are
        excluded — the count difference is surfaced by the synthesis
        prompt as "N unclassified".
        """
        try:
            from worldkg_nca.models import OsmEntity
            cc = (country_code or "").upper() or None
            qs = (
                OsmEntity.objects.using("vectors")
                .filter(
                    osm_id__in=osm_ids,
                    snapshot_id=snapshot_id,
                )
                .exclude(wkg_class__isnull=True)
                .exclude(wkg_class="")
                .values("wkg_class")
                .annotate(count=Count("osm_id"))
                .order_by("-count")
            )
            if cc:
                qs = qs.filter(country_code=cc)
            return list(qs)
        except Exception as exc:  # noqa: BLE001 — fail-soft per source
            logger.warning("Class distribution fetch failed: %s", exc)
            return []
