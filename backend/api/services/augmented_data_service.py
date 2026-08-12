"""
Augmented Data Service.

Builds a comprehensive summary of spatial link predictions for a country,
including accepted/rejected links grouped by subgraph, augmentation
estimates using Google Places API geocoding, entity counts, link type
breakdowns (geo, name, class), and confidence score distributions.

Consumed by:
    GET /api/data/augmented-summary/{country_name}/

Pattern follows AppStateService (api/services/app_state_service.py).

═ Django ORM Query Optimization ═══════════════════════════════════════════
This service uses Django's `aggregate()` / `annotate()` / `values()` ORM
methods to push all counting, bucketing, and classification logic into SQL
rather than iterating millions of rows in Python.

Key patterns used:

  1. `aggregate()` with `Case/When` — replaces Python loops that count rows
     matching conditional logic (e.g., "how many links are geo-dominant?").
     SQL CASE WHEN ... THEN 1 END inside COUNT() is evaluated by the database
     engine in a single pass over the filtered queryset.

  2. `values('relation').annotate(count=Count('id'))` — replaces Python
     Counter loops that group rows by a categorical field. SQL GROUP BY
     does the grouping; Django maps it to a list of dicts.

  3. Combined aggregate — instead of 4 separate queries (count, breakdown,
     score_dist, avg_conf), we issue ONE `aggregate()` call with all
     metrics. This reduces per-subgraph queries from ~4 to 1.

  4. `F()` expressions — allow SQL-level arithmetic on columns (e.g.,
     `geo_score > name_score + topo_score`) without loading rows into Python.

The cross-database spatial query (OsmEntity is in the `vectors` DB while
SpatialTripletScore is in `default`) still requires a two-step fetch
(entity IDs first, then filter), but the subsequent aggregation is now
a single SQL query per subgraph instead of a Python iterator.

═ USLP Data Flow — phases 4 & 5 ═══════════════════════════════════════════

This service implements phases 4 & 5 of the USLP data flow (phases 1–3 are
in igea/services/spatial_link_prediction.py):

+--------+---------------------------------------------------------------+----------------------------------------------------------+
| Phase  | What happens                                                  | Where                                                    |
+========+===============================================================+==========================================================+
| 4      | Dashboard query filters rows:                                | AugmentedDataService.get_summary()                       |
|        | filter(country_name=..., snapshot_id=..., predicted=True)    | → filter() uses igea_triplet_csp_idx (Index Only Scan)   |
+--------+---------------------------------------------------------------+----------------------------------------------------------+
| 5      | Single aggregate() with Case/When reads score columns        | _aggregate_accepted_metrics()                            |
|        | for geo/name/class dominance, histogram, avg confidence      | runs on filtered ~20k rows (Seq Scan over score cols)    |
+--------+---------------------------------------------------------------+----------------------------------------------------------+

Full 5-phase flow:
  1. Score calculation  → predict_links_batch()          (CPU/GPU)
  2. Persist            → persist_links()                (vectors DB)
  3. Index              → Migration igea.0003            (vectors DB)
  4. Query (this svc)   → get_summary()                  (default DB)
  5. Aggregate (this)   → _aggregate_accepted_metrics()  (default DB)

The composite index (phase 3) speeds up phase 4 only.
The math (phases 1, 5) is identical regardless of the index.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────


@dataclass
class LinkTypeBreakdown:
    """Decomposed scores by link type (geo, name, class dominance)."""
    geo_dominant: int = 0
    name_dominant: int = 0
    class_dominant: int = 0
    mixed: int = 0


@dataclass
class ScoreDistribution:
    """Histogram of normalized scores across buckets."""
    buckets: list
    counts: list


@dataclass
class SubgraphLinkGroup:
    """Links grouped by subgraph."""
    subgraph_slug: str
    subgraph_name: str
    accepted_count: int
    rejected_count: int
    link_type_breakdown: LinkTypeBreakdown
    score_distribution: ScoreDistribution
    avg_confidence: float


@dataclass
class AugmentationEstimate:
    """Estimate of how many rejected links could be augmented via Google Places."""
    total_rejected: int
    estimated_augmentable: int
    augmentable_pct: float
    cost_per_call: float
    estimated_total_cost: float
    by_relation: list
    reasoning: str


@dataclass
class AugmentedDataSummary:
    """Full response for GET /api/data/augmented-summary/{country_name}/"""
    country_name: str
    iso: Optional[str]
    total_accepted: int
    total_rejected: int
    acceptance_rate: float
    total_entities: int
    entity_count_with_wkg_class: int
    subgraph_groups: list
    link_type_breakdown: LinkTypeBreakdown
    score_distribution: ScoreDistribution
    augmentation_estimate: AugmentationEstimate
    relation_distribution: list


# ── Service ────────────────────────────────────────────────────────────────


class AugmentedDataService:
    """Builds augmented data summary for a country."""

    # Buckets for normalized score histogram (0.0–1.0 in 0.2 increments)
    BUCKET_EDGES = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    BUCKET_LABELS = ['0.0-0.2', '0.2-0.4', '0.4-0.6', '0.6-0.8', '0.8-1.0']

    # Thresholds for classifying a link's dominant score type
    # If one score exceeds the sum of the others by this ratio, it's dominant
    DOMINANCE_RATIO = 0.5

    def get_summary(self, country_name: str, snapshot_date: Optional[str] = None) -> AugmentedDataSummary:
        """Build the full augmented data summary.

        Args:
            country_name: Country name or ISO code.
            snapshot_date: Optional snapshot date string (e.g. "2025_12_31").
                When provided, entity counts are filtered to that snapshot.
                Spatial link counts are also filtered when the
                SpatialTripletScore.snapshot_id column is populated.
        """
        iso = self._resolve_iso(country_name)
        subgraph_groups = self._build_subgraph_groups(country_name, iso, snapshot_date)
        entity_counts = self._count_entities(country_name, iso, snapshot_date)
        augmentation_estimate = self._estimate_augmentation(country_name, iso)

        # Aggregate across all subgraphs
        total_accepted = sum(sg.accepted_count for sg in subgraph_groups)
        total_rejected = sum(sg.rejected_count for sg in subgraph_groups)
        total_links = total_accepted + total_rejected
        acceptance_rate = round(total_accepted / total_links, 3) if total_links > 0 else 0.0

        # Aggregate link type breakdown
        agg_breakdown = LinkTypeBreakdown()
        for sg in subgraph_groups:
            agg_breakdown.geo_dominant += sg.link_type_breakdown.geo_dominant
            agg_breakdown.name_dominant += sg.link_type_breakdown.name_dominant
            agg_breakdown.class_dominant += sg.link_type_breakdown.class_dominant
            agg_breakdown.mixed += sg.link_type_breakdown.mixed

        # Aggregate score distribution
        agg_score_counts = Counter()
        for sg in subgraph_groups:
            for bucket, count in zip(sg.score_distribution.buckets, sg.score_distribution.counts):
                agg_score_counts[bucket] += count
        agg_score_dist = ScoreDistribution(
            buckets=self.BUCKET_LABELS,
            counts=[agg_score_counts.get(b, 0) for b in self.BUCKET_LABELS],
        )

        # Aggregate relation distribution across all links
        relation_dist = self._build_relation_distribution(country_name, iso, snapshot_date)

        return AugmentedDataSummary(
            country_name=country_name,
            iso=iso,
            total_accepted=total_accepted,
            total_rejected=total_rejected,
            acceptance_rate=acceptance_rate,
            total_entities=entity_counts['total'],
            entity_count_with_wkg_class=entity_counts['with_wkg_class'],
            subgraph_groups=[
                {
                    'subgraph_slug': sg.subgraph_slug,
                    'subgraph_name': sg.subgraph_name,
                    'accepted_count': sg.accepted_count,
                    'rejected_count': sg.rejected_count,
                    'link_type_breakdown': {
                        'geo_dominant': sg.link_type_breakdown.geo_dominant,
                        'name_dominant': sg.link_type_breakdown.name_dominant,
                        'class_dominant': sg.link_type_breakdown.class_dominant,
                        'mixed': sg.link_type_breakdown.mixed,
                    },
                    'score_distribution': {
                        'buckets': sg.score_distribution.buckets,
                        'counts': sg.score_distribution.counts,
                    },
                    'avg_confidence': sg.avg_confidence,
                }
                for sg in subgraph_groups
            ],
            link_type_breakdown={
                'geo_dominant': agg_breakdown.geo_dominant,
                'name_dominant': agg_breakdown.name_dominant,
                'class_dominant': agg_breakdown.class_dominant,
                'mixed': agg_breakdown.mixed,
            },
            score_distribution={
                'buckets': agg_score_dist.buckets,
                'counts': agg_score_dist.counts,
            },
            augmentation_estimate={
                'total_rejected': augmentation_estimate.total_rejected,
                'estimated_augmentable': augmentation_estimate.estimated_augmentable,
                'augmentable_pct': augmentation_estimate.augmentable_pct,
                'cost_per_call': augmentation_estimate.cost_per_call,
                'estimated_total_cost': augmentation_estimate.estimated_total_cost,
                'by_relation': augmentation_estimate.by_relation,
                'reasoning': augmentation_estimate.reasoning,
            },
            relation_distribution=relation_dist,
        )

    # ── Subgraph Groups ────────────────────────────────────────────────

    def _build_subgraph_groups(
        self, country_name: str, iso: Optional[str], snapshot_date: Optional[str] = None
    ) -> list[SubgraphLinkGroup]:
        """Build per-subgraph link groupings from SpatialTripletScore data.

        Uses SQL-level aggregation instead of Python iteration. For each
        subgraph, we issue ONE aggregate query that computes:
          - accepted/rejected counts
          - link type breakdown (geo/name/class/mixed dominant)
          - score distribution histogram (5 buckets)
          - average confidence

        This replaces the previous approach of iterating millions of rows
        in Python with `qs.iterator(chunk_size=5000)` per metric.
        """
        from django.db.models import Q
        from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        snapshot_filter = self._snapshot_filter(snapshot_date)

        subgraphs = self._get_subgraphs(iso)
        groups = []

        if subgraphs:
            for sg in subgraphs:
                sg_slug = sg.slug if sg.slug else ''
                sg_name = sg.name if sg.name else sg_slug

                # Fetch head entity IDs within this subgraph's bbox.
                # Cross-database constraint: OsmEntity lives in the `vectors`
                # DB while SpatialTripletScore lives in `default`, so we
                # can't use a SQL subquery. We fetch entity IDs first,
                # then filter the link queryset by head_osm_id__in.
                entity_ids = self._get_subgraph_entity_ids(sg)
                if not entity_ids:
                    continue

                # Base querysets scoped to this subgraph's entities
                accepted_qs = SpatialTripletScore.objects.filter(
                    country_filter & Q(predicted=True) & snapshot_filter
                    & Q(head_osm_id__in=entity_ids)
                )
                rejected_count = SpatialTripletScoreRejected.objects.filter(
                    country_filter & snapshot_filter
                    & Q(head_osm_id__in=entity_ids)
                ).count()

                accepted_count = accepted_qs.count()
                if accepted_count == 0 and rejected_count == 0:
                    continue

                # Single aggregate query for all accepted-link metrics.
                # This replaces 3 separate Python iterator loops
                # (breakdown, score_dist, avg_conf) with one SQL pass.
                agg = self._aggregate_accepted_metrics(accepted_qs)

                groups.append(SubgraphLinkGroup(
                    subgraph_slug=sg_slug,
                    subgraph_name=sg_name,
                    accepted_count=accepted_count,
                    rejected_count=rejected_count,
                    link_type_breakdown=agg['breakdown'],
                    score_distribution=ScoreDistribution(
                        buckets=self.BUCKET_LABELS,
                        counts=agg['score_counts'],
                    ),
                    avg_confidence=agg['avg_confidence'],
                ))

        # If no subgraph groups or no subgraph profiles found,
        # return a single country-level group (no spatial filtering needed)
        if not groups:
            accepted_qs = SpatialTripletScore.objects.filter(
                country_filter & Q(predicted=True) & snapshot_filter
            )
            rejected_count = SpatialTripletScoreRejected.objects.filter(
                country_filter & snapshot_filter
            ).count()

            accepted_count = accepted_qs.count()
            if accepted_count > 0 or rejected_count > 0:
                agg = self._aggregate_accepted_metrics(accepted_qs)

                groups.append(SubgraphLinkGroup(
                    subgraph_slug='country-level',
                    subgraph_name=f'{country_name} (country-level)',
                    accepted_count=accepted_count,
                    rejected_count=rejected_count,
                    link_type_breakdown=agg['breakdown'],
                    score_distribution=ScoreDistribution(
                        buckets=self.BUCKET_LABELS,
                        counts=agg['score_counts'],
                    ),
                    avg_confidence=agg['avg_confidence'],
                ))

        return groups

    def _aggregate_accepted_metrics(self, accepted_qs) -> dict:
        """Compute all accepted-link metrics in a single SQL aggregate query.

        Phase 5 of USLP data flow:
        +--------+---------------------------------------------------------------+----------------------------------------------------------+
        | Phase  | What happens                                                  | Where                                                    |
        +========+===============================================================+==========================================================+
        | 5      | Single aggregate() with Case/When reads score columns        | _aggregate_accepted_metrics()                            |
        |        | for geo/name/class dominance, histogram, avg confidence      | runs on filtered ~20k rows (Seq Scan over score cols)    |
        +--------+---------------------------------------------------------------+----------------------------------------------------------+

        This replaces 3 separate Python iterator loops with one database
        round-trip. The SQL engine evaluates all CASE WHEN expressions
        in a single pass over the filtered rows.

        Returns:
            {
                'breakdown': LinkTypeBreakdown,
                'score_counts': [int, int, int, int, int],  # 5 buckets
                'avg_confidence': float,
            }
        """
        from django.db.models import Avg, Case, Count, F, When

        # Link type dominance classification:
        #   geo_dominant   → geo_score > name_score + topo_score
        #   name_dominant  → name_score > geo_score + topo_score
        #   class_dominant → topo_score > geo_score + name_score
        #   mixed          → none of the above (including all-zero scores)
        #
        # The ratio test `geo_score / total > 0.5` is algebraically
        # equivalent to `geo_score > total - geo_score` i.e.
        # `geo_score > name_score + topo_score`. Using F() expressions
        # lets SQL do this arithmetic without loading rows into Python.
        result = accepted_qs.aggregate(
            total=Count('id'),
            geo_dominant=Count(
                Case(When(geo_score__gt=F('name_score') + F('topo_score'), then=1))
            ),
            name_dominant=Count(
                Case(When(name_score__gt=F('geo_score') + F('topo_score'), then=1))
            ),
            class_dominant=Count(
                Case(When(topo_score__gt=F('geo_score') + F('name_score'), then=1))
            ),
            # Score distribution histogram — 5 fixed buckets over [0.0, 1.0]
            # The last bucket includes 1.0 (>= 0.8 rather than range 0.8-1.0)
            # to catch the edge case where normalized_score is exactly 1.0.
            b0=Count(Case(When(normalized_score__gte=0.0, normalized_score__lt=0.2, then=1))),
            b1=Count(Case(When(normalized_score__gte=0.2, normalized_score__lt=0.4, then=1))),
            b2=Count(Case(When(normalized_score__gte=0.4, normalized_score__lt=0.6, then=1))),
            b3=Count(Case(When(normalized_score__gte=0.6, normalized_score__lt=0.8, then=1))),
            b4=Count(Case(When(normalized_score__gte=0.8, then=1))),
            avg_confidence=Avg('normalized_score'),
        )

        total = result['total'] or 0
        geo = result['geo_dominant'] or 0
        name = result['name_dominant'] or 0
        cls = result['class_dominant'] or 0
        # mixed = total - (geo + name + cls) — captures both
        # "no dominant component" and "all scores are zero"
        mixed = total - geo - name - cls

        avg_conf = result['avg_confidence']
        return {
            'breakdown': LinkTypeBreakdown(
                geo_dominant=geo,
                name_dominant=name,
                class_dominant=cls,
                mixed=mixed,
            ),
            'score_counts': [
                result['b0'] or 0,
                result['b1'] or 0,
                result['b2'] or 0,
                result['b3'] or 0,
                result['b4'] or 0,
            ],
            'avg_confidence': round(avg_conf, 4) if avg_conf else 0.0,
        }

    # ── Entity Counts ──────────────────────────────────────────────────

    @staticmethod
    def _count_entities(country_name: str, iso: Optional[str], snapshot_date: Optional[str] = None) -> dict:
        """Count total entities and entities with WorldKG class.

        When snapshot_date is provided, counts are filtered to that snapshot
        via the OsmEntity.snapshot_id column (CharField, e.g. "2025_12_31").
        """
        from django.db import connections

        result = {'total': 0, 'with_wkg_class': 0}

        try:
            with connections['vectors'].cursor() as cursor:
                if snapshot_date:
                    cursor.execute(
                        "SELECT COUNT(*) FROM semantic_search_osmentity WHERE snapshot_id = %s",
                        [snapshot_date],
                    )
                    result['total'] = cursor.fetchone()[0]
                    cursor.execute(
                        "SELECT COUNT(*) FROM semantic_search_osmentity "
                        "WHERE snapshot_id = %s AND wkg_class IS NOT NULL",
                        [snapshot_date],
                    )
                    result['with_wkg_class'] = cursor.fetchone()[0]
                else:
                    cursor.execute("SELECT COUNT(*) FROM semantic_search_osmentity")
                    result['total'] = cursor.fetchone()[0]
                    cursor.execute(
                        "SELECT COUNT(*) FROM semantic_search_osmentity WHERE wkg_class IS NOT NULL"
                    )
                    result['with_wkg_class'] = cursor.fetchone()[0]
        except Exception as exc:
            logger.warning(f"Failed to count entities for {country_name}: {exc}")

        return result

    # ── Relation Distribution ──────────────────────────────────────────

    def _build_relation_distribution(
        self, country_name: str, iso: Optional[str], snapshot_date: Optional[str] = None
    ) -> list:
        """Build relation-level distribution with acceptance rates.

        Uses SQL GROUP BY via `values('relation').annotate(count=Count('id'))`
        instead of iterating all accepted/rejected rows in Python. This
        reduces two full-table scans to two GROUP BY queries that return
        one row per relation (typically 10-50 rows).
        """
        from django.db.models import Count, Q
        from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        snapshot_filter = self._snapshot_filter(snapshot_date)

        # SQL: SELECT relation, COUNT(*) FROM ... GROUP BY relation
        # Returns one dict per relation: {'relation': str, 'count': int}
        accepted_by_rel = (
            SpatialTripletScore.objects
            .filter(country_filter & Q(predicted=True) & snapshot_filter)
            .values('relation')
            .annotate(count=Count('id'))
        )
        rejected_by_rel = (
            SpatialTripletScoreRejected.objects
            .filter(country_filter & snapshot_filter)
            .values('relation')
            .annotate(count=Count('id'))
        )

        # Merge accepted + rejected counts per relation in Python.
        # This is a small dict merge (one entry per relation), not a
        # row-by-row iteration — the heavy lifting is done by SQL GROUP BY.
        rel_accepted = {r['relation']: r['count'] for r in accepted_by_rel}
        rel_rejected = {r['relation']: r['count'] for r in rejected_by_rel}

        all_relations = set(rel_accepted) | set(rel_rejected)

        distribution = []
        for rel in all_relations:
            acc = rel_accepted.get(rel, 0)
            rej = rel_rejected.get(rel, 0)
            total = acc + rej
            distribution.append({
                'relation': rel,
                'count': total,
                'accepted': acc,
                'rejected': rej,
                'acceptance_rate': round(acc / total, 3) if total > 0 else 0.0,
            })

        # Sort by total count descending (matches the old Counter.most_common())
        distribution.sort(key=lambda x: x['count'], reverse=True)
        return distribution

    # ── Augmentation Estimate ──────────────────────────────────────────

    def _estimate_augmentation(
        self, country_name: str, iso: Optional[str]
    ) -> AugmentationEstimate:
        """Estimate how many rejected links could be augmented via Google Places API.

        Uses the following heuristic:
        - Rejected links with geo_score > 0.3 have good spatial proximity but weak
          name/class match — these are good candidates for Places API geocoding
          (the API can resolve the correct entity by location).
        - Rejected links with name_score > 0.3 have good name match but wrong
          location — these may or may not be augmentable (Places can help if the
          entity exists in their DB).
        - Rejected links with class_score > 0.3 have good class match — these are
          plausible candidates.
        - Links where ALL scores are < 0.2 are unlikely to be augmentable.

        Implementation: SQL `aggregate()` with `Case/When` for the total
        count, and `values('relation').annotate()` for the per-relation
        breakdown. This replaces iterating all rejected links in Python.
        """
        from django.db.models import Case, Count, Q, When
        from igea.models import SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        rejected_qs = SpatialTripletScoreRejected.objects.filter(country_filter)

        # Single aggregate query: total count + augmentable count.
        # The augmentable condition (geo > 0.3 OR name > 0.3 OR topo > 0.3)
        # is pushed to SQL via CASE WHEN with Q expressions.
        totals = rejected_qs.aggregate(
            total=Count('id'),
            augmentable=Count(
                Case(When(
                    Q(geo_score__gt=0.3) | Q(name_score__gt=0.3) | Q(topo_score__gt=0.3),
                    then=1,
                ))
            ),
        )

        total_rejected = totals['total'] or 0
        augmentable_count = totals['augmentable'] or 0

        if total_rejected == 0:
            return AugmentationEstimate(
                total_rejected=0,
                estimated_augmentable=0,
                augmentable_pct=0.0,
                cost_per_call=0.005,
                estimated_total_cost=0.0,
                by_relation=[],
                reasoning="No rejected links to augment.",
            )

        cost_per_call = 0.005  # Google Places Geocoding starter tier

        # Per-relation breakdown via SQL GROUP BY + conditional COUNT.
        # This returns one row per relation with total and augmentable counts,
        # replacing the Python Counter loop over all rejected links.
        by_rel_qs = (
            rejected_qs
            .values('relation')
            .annotate(
                total=Count('id'),
                augmentable=Count(
                    Case(When(
                        Q(geo_score__gt=0.3) | Q(name_score__gt=0.3) | Q(topo_score__gt=0.3),
                        then=1,
                    ))
                ),
            )
            .order_by('-total')
        )

        by_relation = []
        for row in by_rel_qs:
            rel_total = row['total']
            rel_aug = row['augmentable']
            by_relation.append({
                'relation': row['relation'],
                'total': rel_total,
                'estimated_augmentable': rel_aug,
                'augmentable_pct': round(rel_aug / rel_total * 100, 1) if rel_total > 0 else 0.0,
                'cost_usd': round(rel_aug * cost_per_call, 2),
            })

        augmentable_pct = round(augmentable_count / total_rejected * 100, 1)
        estimated_total_cost = round(augmentable_count * cost_per_call, 2)

        reasoning = (
            f"Of {total_rejected:,} rejected links, ~{augmentable_count:,} ({augmentable_pct}%) "
            f"have at least one score component (geo, name, or class) above 0.3, "
            f"indicating they are plausible candidates for Google Places geocoding validation. "
            f"Estimated cost: ${estimated_total_cost:.2f} (geocoding @ ${cost_per_call:.3f}/call)."
        )

        return AugmentationEstimate(
            total_rejected=total_rejected,
            estimated_augmentable=augmentable_count,
            augmentable_pct=augmentable_pct,
            cost_per_call=cost_per_call,
            estimated_total_cost=estimated_total_cost,
            by_relation=by_relation,
            reasoning=reasoning,
        )

    # ── Subgraph Resolution ────────────────────────────────────────────

    @staticmethod
    def _get_subgraphs(iso: Optional[str]) -> list:
        """Get subgraph profiles for a country."""
        if not iso:
            return []

        from orchestration.models import CountryPipelineProfile, SubgraphProfile

        profile = CountryPipelineProfile.objects.filter(
            iso2__iexact=iso
        ).first()

        if not profile:
            return []

        return list(
            SubgraphProfile.objects.filter(country_profile=profile)
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _country_filter(country_name: str, iso: Optional[str]):
        """Build a Q filter matching country_name or ISO code."""
        from django.db.models import Q

        f = Q(country_name__iexact=country_name)
        if iso and iso.upper() != country_name.upper():
            f |= Q(country_name__iexact=iso)
        return f

    @staticmethod
    def _snapshot_filter(snapshot_date: Optional[str]):
        """Build a Q filter for SpatialTripletScore.snapshot_id.

        SpatialTripletScore.snapshot_id is a CharField (YYYY_MM_DD) that
        matches OsmEntity.snapshot_id.  When snapshot_date is provided,
        filter by it directly.  When not provided, return a no-op.
        """
        from django.db.models import Q

        if snapshot_date:
            return Q(snapshot_id=snapshot_date)
        return Q()

    @staticmethod
    def _resolve_iso(country_name: str) -> Optional[str]:
        """Resolve ISO code from country name."""
        from orchestration.models import CountryPipelineProfile

        profile = CountryPipelineProfile.objects.filter(
            canonical_name__iexact=country_name
        ).first()
        if profile and profile.iso2:
            return profile.iso2

        from extraction.services.osm_wikidata_resolver import resolve_iso_code
        return resolve_iso_code(country_name)

    @staticmethod
    def _get_subgraph_entity_ids(subgraph_profile) -> list:
        """Get OSM entity IDs within a subgraph's bbox (approximate).

        Returns up to 10,000 entity IDs for scoping.
        """
        bbox = (
            subgraph_profile.bbox_min_lat is not None
            and subgraph_profile.bbox_max_lat is not None
            and subgraph_profile.bbox_min_lon is not None
            and subgraph_profile.bbox_max_lon is not None
        )
        if not bbox:
            return []

        from worldkg_nca.models import OsmEntity

        return list(
            OsmEntity.objects.using('vectors')
            .filter(
                geom__within=(
                    f'POLYGON(({subgraph_profile.bbox_min_lon} {subgraph_profile.bbox_min_lat}, '
                    f'{subgraph_profile.bbox_max_lon} {subgraph_profile.bbox_min_lat}, '
                    f'{subgraph_profile.bbox_max_lon} {subgraph_profile.bbox_max_lat}, '
                    f'{subgraph_profile.bbox_min_lon} {subgraph_profile.bbox_max_lat}, '
                    f'{subgraph_profile.bbox_min_lon} {subgraph_profile.bbox_min_lat}))'
                ),
                osm_type='node',
            )
            .values_list('osm_id', flat=True)[:10000]
        )

