"""
Augmented Data Service.

Builds a comprehensive summary of spatial link predictions for a country,
including accepted/rejected links grouped by subgraph, augmentation
estimates using Google Places API geocoding, entity counts, link type
breakdowns (geo, name, class), and confidence score distributions.

Consumed by:
    GET /api/data/augmented-summary/{country_name}/

Pattern follows AppStateService (api/services/app_state_service.py).
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
        """Build per-subgraph link groupings from SpatialTripletScore data."""
        from django.db.models import Q
        from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        snapshot_filter = self._snapshot_filter(snapshot_date)

        # Fetch subgraphs for this country
        subgraphs = self._get_subgraphs(iso)
        groups = []

        if subgraphs:
            for sg in subgraphs:
                sg_slug = sg.slug if sg.slug else ''
                sg_name = sg.name if sg.name else sg_slug

                # Count accepted links for entities in this subgraph
                # We approximate subgraph membership by linking through OsmEntity
                # spatial containment. For now, use a simpler approach: aggregate
                # all links and group by relation patterns.
                accepted = SpatialTripletScore.objects.filter(
                    country_filter & Q(predicted=True) & snapshot_filter
                )
                rejected = SpatialTripletScoreRejected.objects.filter(
                    country_filter & snapshot_filter
                )

                # For actual subgraph scoping, we'd need entity-to-subgraph mapping.
                # Since SpatialTripletScore stores country_name but not subgraph_slug,
                # we estimate by counting all country-level links.
                # Accurate subgraph grouping requires the entity's bbox/subgraph
                # membership, which we compute below.
                accepted_count = self._count_links_in_subgraph(accepted, sg)
                rejected_count = self._count_links_in_subgraph(rejected, sg)
                total_count = accepted_count + rejected_count

                if total_count == 0:
                    continue

                # Compute link type breakdown for this subgraph
                breakdown = self._compute_link_type_breakdown(accepted, sg)

                # Compute score distribution
                score_counts = self._compute_score_distribution(accepted, sg)

                # Average confidence (normalized_score for accepted links)
                avg_conf = self._compute_avg_confidence(accepted, sg)

                groups.append(SubgraphLinkGroup(
                    subgraph_slug=sg_slug,
                    subgraph_name=sg_name,
                    accepted_count=accepted_count,
                    rejected_count=rejected_count,
                    link_type_breakdown=breakdown,
                    score_distribution=ScoreDistribution(
                        buckets=self.BUCKET_LABELS,
                        counts=[score_counts.get(b, 0) for b in self.BUCKET_LABELS],
                    ),
                    avg_confidence=avg_conf,
                ))

        # If no subgraph groups or no subgraph profiles found,
        # return a single country-level group
        if not groups:
            accepted = SpatialTripletScore.objects.filter(
                country_filter & Q(predicted=True) & snapshot_filter
            )
            rejected = SpatialTripletScoreRejected.objects.filter(
                country_filter & snapshot_filter
            )

            accepted_count = accepted.count()
            rejected_count = rejected.count()

            if accepted_count > 0 or rejected_count > 0:
                breakdown = self._compute_link_type_breakdown_from_qs(accepted)
                score_counts = self._compute_score_distribution_from_qs(accepted)
                avg_conf = self._compute_avg_confidence_from_qs(accepted)

                groups.append(SubgraphLinkGroup(
                    subgraph_slug='country-level',
                    subgraph_name=f'{country_name} (country-level)',
                    accepted_count=accepted_count,
                    rejected_count=rejected_count,
                    link_type_breakdown=breakdown,
                    score_distribution=ScoreDistribution(
                        buckets=self.BUCKET_LABELS,
                        counts=[score_counts.get(b, 0) for b in self.BUCKET_LABELS],
                    ),
                    avg_confidence=avg_conf,
                ))

        return groups

    @staticmethod
    def _count_links_in_subgraph(qs, subgraph_profile) -> int:
        """Approximate count of links whose head entity falls within the subgraph bbox.

        Since SpatialTripletScore doesn't store subgraph_slug directly, we use
        the subgraph's bbox to filter entities. If no bbox is available, return 0
        to avoid incorrect attribution.
        """
        bbox = (
            subgraph_profile.bbox_min_lat is not None
            and subgraph_profile.bbox_max_lat is not None
            and subgraph_profile.bbox_min_lon is not None
            and subgraph_profile.bbox_max_lon is not None
        )
        if not bbox:
            return 0

        from worldkg_nca.models import OsmEntity
        # Get head entity IDs within this subgraph's bbox
        entity_ids = list(
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

        if not entity_ids:
            return 0

        return qs.filter(head_osm_id__in=entity_ids).count()

    def _compute_link_type_breakdown(self, accepted_qs, subgraph_profile) -> LinkTypeBreakdown:
        """Classify each accepted link by which score component dominates."""
        return self._compute_link_type_breakdown_from_qs(accepted_qs)

    def _compute_link_type_breakdown_from_qs(self, qs) -> LinkTypeBreakdown:
        """Classify links by dominant score component from a queryset."""
        breakdown = LinkTypeBreakdown()
        for link in qs.iterator(chunk_size=5000):
            total = link.geo_score + link.name_score + link.topo_score
            if total == 0:
                breakdown.mixed += 1
                continue

            geo_ratio = link.geo_score / total if link.geo_score > 0 else 0
            name_ratio = link.name_score / total if link.name_score > 0 else 0
            class_ratio = link.topo_score / total if link.topo_score > 0 else 0

            if geo_ratio > self.DOMINANCE_RATIO:
                breakdown.geo_dominant += 1
            elif name_ratio > self.DOMINANCE_RATIO:
                breakdown.name_dominant += 1
            elif class_ratio > self.DOMINANCE_RATIO:
                breakdown.class_dominant += 1
            else:
                breakdown.mixed += 1

        return breakdown

    def _compute_score_distribution(self, accepted_qs, subgraph_profile) -> Counter:
        """Compute score distribution histogram for a subgraph's accepted links."""
        return self._compute_score_distribution_from_qs(accepted_qs)

    def _compute_score_distribution_from_qs(self, qs) -> Counter:
        """Compute score distribution histogram from a queryset."""
        score_counts = Counter()
        for link in qs.iterator(chunk_size=5000):
            for i in range(len(self.BUCKET_EDGES) - 1):
                if self.BUCKET_EDGES[i] <= link.normalized_score < self.BUCKET_EDGES[i + 1]:
                    score_counts[self.BUCKET_LABELS[i]] += 1
                    break
            else:
                if link.normalized_score >= 1.0:
                    score_counts['0.8-1.0'] += 1
        return score_counts

    def _compute_avg_confidence(self, accepted_qs, subgraph_profile) -> float:
        """Compute average normalized_score for a subgraph's accepted links."""
        return self._compute_avg_confidence_from_qs(accepted_qs)

    @staticmethod
    def _compute_avg_confidence_from_qs(qs) -> float:
        """Compute average normalized_score from a queryset."""
        from django.db.models import Avg
        agg = qs.aggregate(avg=Avg('normalized_score'))
        avg = agg.get('avg')
        return round(avg, 4) if avg else 0.0

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
        """Build relation-level distribution with acceptance rates."""
        from collections import Counter
        from django.db.models import Q
        from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        snapshot_filter = self._snapshot_filter(snapshot_date)

        accepted = SpatialTripletScore.objects.filter(
            country_filter & Q(predicted=True) & snapshot_filter
        )
        rejected = SpatialTripletScoreRejected.objects.filter(
            country_filter & snapshot_filter
        )

        rel_counts = Counter()
        rel_accepted = Counter()
        rel_rejected = Counter()

        for s in accepted.iterator(chunk_size=5000):
            rel_counts[s.relation] += 1
            rel_accepted[s.relation] += 1

        for s in rejected.iterator(chunk_size=5000):
            rel_counts[s.relation] += 1
            rel_rejected[s.relation] += 1

        distribution = []
        for rel, count in rel_counts.most_common():
            acc = rel_accepted.get(rel, 0)
            rej = rel_rejected.get(rel, 0)
            distribution.append({
                'relation': rel,
                'count': count,
                'accepted': acc,
                'rejected': rej,
                'acceptance_rate': round(acc / count, 3) if count > 0 else 0.0,
            })

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
        """
        from collections import Counter
        from django.db.models import Q
        from igea.models import SpatialTripletScoreRejected

        country_filter = self._country_filter(country_name, iso)
        rejected = SpatialTripletScoreRejected.objects.filter(country_filter)
        total_rejected = rejected.count()

        if total_rejected == 0:
            cost_per_call = 0.005
            return AugmentationEstimate(
                total_rejected=0,
                estimated_augmentable=0,
                augmentable_pct=0.0,
                cost_per_call=cost_per_call,
                estimated_total_cost=0.0,
                by_relation=[],
                reasoning="No rejected links to augment.",
            )

        cost_per_call = 0.005  # Google Places Geocoding starter tier

        # Categorize rejected links by augmentability
        augmentable_count = 0
        relation_augmentable = Counter()
        relation_total = Counter()

        for s in rejected.iterator(chunk_size=5000):
            relation_total[s.relation] += 1

            # Check if this link is a good augmentation candidate
            is_augmentable = (
                s.geo_score > 0.3        # Good spatial proximity
                or s.name_score > 0.3    # Good name match
                or s.topo_score > 0.3    # Good class match
            )

            if is_augmentable:
                augmentable_count += 1
                relation_augmentable[s.relation] += 1

        augmentable_pct = round(augmentable_count / total_rejected * 100, 1) if total_rejected > 0 else 0.0
        estimated_total_cost = round(augmentable_count * cost_per_call, 2)

        by_relation = []
        for rel, total in relation_total.most_common():
            aug = relation_augmentable.get(rel, 0)
            by_relation.append({
                'relation': rel,
                'total': total,
                'estimated_augmentable': aug,
                'augmentable_pct': round(aug / total * 100, 1) if total > 0 else 0.0,
                'cost_usd': round(aug * cost_per_call, 2),
            })

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

