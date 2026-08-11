"""
Augmented Data API Views.

Provides endpoints for:
1. GET /api/data/augmented-summary/{country_name}/
   — Comprehensive summary of spatial link predictions, augmentation estimates,
     entity counts, link type breakdowns, and score distributions.

Pattern follows views_worldkg_pipeline.py (WorldKGPipelineSummaryView et al.)
and views_app_state.py (AppStateView).
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

logger = logging.getLogger(__name__)


class AugmentedDataSummaryView(APIView):
    """
    GET /api/data/augmented-summary/{country_name}/

    Returns a comprehensive summary of spatial link predictions for a country,
    including:
    - Accepted/rejected link counts per subgraph
    - Link type breakdown (geo-dominant, name-dominant, class-dominant, mixed)
    - Score distribution histogram (0.0–1.0 in 0.2 buckets)
    - Augmentation estimate via Google Places geocoding
    - Entity counts (total and with WorldKG class)
    - Relation distribution with acceptance rates

    Query params:
        snapshot_date - Optional. Snapshot date string (e.g. "2025_12_31").
                        Accepted for forward compatibility; the data layer
                        currently returns the latest run's data regardless.
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # snapshot_date filters entity counts by OsmEntity.snapshot_id
        snapshot_date = request.query_params.get('snapshot_date')

        try:
            from api.services.augmented_data_service import AugmentedDataService
            service = AugmentedDataService()
            summary = service.get_summary(country_name, snapshot_date=snapshot_date)

            return Response({
                'country_name': summary.country_name,
                'iso': summary.iso,
                'total_accepted': summary.total_accepted,
                'total_rejected': summary.total_rejected,
                'acceptance_rate': summary.acceptance_rate,
                'total_entities': summary.total_entities,
                'entity_count_with_wkg_class': summary.entity_count_with_wkg_class,
                'subgraph_groups': summary.subgraph_groups,
                'link_type_breakdown': summary.link_type_breakdown,
                'score_distribution': summary.score_distribution,
                'augmentation_estimate': summary.augmentation_estimate,
                'relation_distribution': summary.relation_distribution,
            })

        except Exception as exc:
            logger.error(
                f"AugmentedDataSummaryView error for {country_name}: {exc}",
                exc_info=True,
            )
            return Response(
                {'error': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AugmentedDataDetailView(APIView):
    """
    GET /api/data/augmented-detail/{country_name}/

    Returns detailed per-entity breakdown of spatial link predictions for a country,
    including:
    - Individual accepted links with full score decomposition (geo, name, topo)
    - Individual rejected links with full score decomposition
    - Entity-level statistics
    - Link type classification for each individual link

    Supports pagination via ?page=1&page_size=100 query params.
    Also accepts ?snapshot_date=2025_12_31 (forward-compatible, not yet filtered).
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # snapshot_date filters entity counts by OsmEntity.snapshot_id
        snapshot_date = request.query_params.get('snapshot_date')

        # Pagination params
        try:
            page = int(request.query_params.get('page', 1))
            page_size = min(int(request.query_params.get('page_size', 100)), 5000)
        except (ValueError, TypeError):
            page = 1
            page_size = 100

        offset = (page - 1) * page_size

        try:
            from django.db.models import Q
            from igea.models import SpatialTripletScore, SpatialTripletScoreRejected
            from api.services.augmented_data_service import AugmentedDataService

            service = AugmentedDataService()
            iso = service._resolve_iso(country_name)

            country_filter = service._country_filter(country_name, iso)
            snapshot_filter = service._snapshot_filter(snapshot_date)

            # Query accepted links with pagination
            accepted_qs = SpatialTripletScore.objects.filter(
                country_filter & Q(predicted=True) & snapshot_filter
            ).order_by('-normalized_score')

            # Query rejected links with pagination
            rejected_qs = SpatialTripletScoreRejected.objects.filter(
                country_filter & snapshot_filter
            ).order_by('-normalized_score')

            total_accepted = accepted_qs.count()
            total_rejected = rejected_qs.count()

            # Fetch paginated slices
            accepted_slice = list(
                accepted_qs[offset:offset + page_size]
            )
            rejected_slice = list(
                rejected_qs[offset:offset + page_size]
            )

            # Serialize accepted links
            accepted_links = []
            for link in accepted_slice:
                total = link.geo_score + link.name_score + link.topo_score
                if total > 0:
                    geo_ratio = link.geo_score / total
                    name_ratio = link.name_score / total
                    class_ratio = link.topo_score / total
                    if geo_ratio > 0.5:
                        dominant_type = 'geo'
                    elif name_ratio > 0.5:
                        dominant_type = 'name'
                    elif class_ratio > 0.5:
                        dominant_type = 'class'
                    else:
                        dominant_type = 'mixed'
                else:
                    dominant_type = 'mixed'

                accepted_links.append({
                    'id': str(link.id),
                    'head_osm_type': link.head_osm_type,
                    'head_osm_id': link.head_osm_id,
                    'tail_osm_type': link.tail_osm_type,
                    'tail_osm_id': link.tail_osm_id,
                    'relation': link.relation,
                    'geo_score': round(link.geo_score, 4),
                    'name_score': round(link.name_score, 4),
                    'topo_score': round(link.topo_score, 4),
                    'unnormalized_score': round(link.unnormalized_score, 4),
                    'normalized_score': round(link.normalized_score, 4),
                    'dominant_type': dominant_type,
                    'created_at': link.created_at.isoformat() if link.created_at else None,
                })

            # Serialize rejected links
            rejected_links = []
            for link in rejected_slice:
                rejected_links.append({
                    'id': str(link.id),
                    'head_osm_type': link.head_osm_type,
                    'head_osm_id': link.head_osm_id,
                    'tail_osm_type': link.tail_osm_type,
                    'tail_osm_id': link.tail_osm_id,
                    'relation': link.relation,
                    'geo_score': round(link.geo_score, 4),
                    'name_score': round(link.name_score, 4),
                    'topo_score': round(link.topo_score, 4),
                    'unnormalized_score': round(link.unnormalized_score, 4),
                    'normalized_score': round(link.normalized_score, 4),
                    'created_at': link.created_at.isoformat() if link.created_at else None,
                })

            return Response({
                'country_name': country_name,
                'iso': iso,
                'total_accepted': total_accepted,
                'total_rejected': total_rejected,
                'page': page,
                'page_size': page_size,
                'has_next': (offset + page_size) < (total_accepted + total_rejected),
                'accepted_links': accepted_links,
                'rejected_links': rejected_links,
            })

        except Exception as exc:
            logger.error(
                f"AugmentedDataDetailView error for {country_name}: {exc}",
                exc_info=True,
            )
            return Response(
                {'error': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AugmentedLinksGeomView(APIView):
    """
    GET /api/data/augmented-links-geom/{country_name}/

    Returns accepted and rejected spatial link predictions with resolved
    head/tail coordinates for map visualization. Links are sampled
    proportionally per relation type (by count) so the map reflects the
    true distribution, then ordered by normalized_score within each
    relation. Total per set capped at ?limit= (default 500).

    Response:
        {
            "accepted": [
                {
                    "head": {"lat": float, "lon": float, "osm_type": str, "osm_id": int},
                    "tail": {"lat": float, "lon": float, "osm_type": str, "osm_id": int},
                    "relation": str,
                    "normalized_score": float,
                },
                ...
            ],
            "rejected": [ ... same structure ... ],
            "total_accepted": int,
            "total_rejected": int,
            "returned_accepted": int,
            "returned_rejected": int,
        }

    Query params:
        snapshot_date - Optional snapshot date string (e.g. "2025_12_31").
        limit         - Max links per set (default 500, max 2000).
    """
    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        country_name = country_name.strip()
        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        snapshot_date = request.query_params.get('snapshot_date')

        try:
            limit = min(int(request.query_params.get('limit', 500)), 2000)
        except (ValueError, TypeError):
            limit = 500

        try:
            from django.db.models import Q
            from igea.models import SpatialTripletScore, SpatialTripletScoreRejected
            from api.services.augmented_data_service import AugmentedDataService
            from worldkg_nca.models import OsmEntity

            service = AugmentedDataService()
            iso = service._resolve_iso(country_name)

            country_filter = service._country_filter(country_name, iso)
            snapshot_filter = service._snapshot_filter(snapshot_date)

            def _sample_proportional(model, base_filter, total_limit):
                """Sample links proportionally per relation type.

                Instead of taking the top-N by score globally (which
                over-represents relation types with higher scores), this
                allocates the limit proportionally across relation types
                by their count, then takes the top-scored links within
                each relation's allocation.
                """
                from django.db.models import Count

                base_qs = model.objects.filter(base_filter)

                # Count per relation
                rel_counts = (
                    base_qs.values('relation')
                    .annotate(c=Count('id'))
                    .order_by('-c')
                )
                total = sum(r['c'] for r in rel_counts)
                if total == 0:
                    return []

                # Allocate limit proportionally, minimum 1 per relation
                links = []
                for rc in rel_counts:
                    alloc = max(1, round((rc['c'] / total) * total_limit))
                    alloc = min(alloc, rc['c'])  # can't take more than exist
                    rel_links = (
                        base_qs.filter(relation=rc['relation'])
                        .order_by('-normalized_score')[:alloc]
                    )
                    links.extend(rel_links)

                # If proportional allocation underfills (rounding), top up
                # from the highest-scoring remaining links
                if len(links) < total_limit:
                    already_ids = {l.id for l in links}
                    extra = (
                        base_qs.exclude(id__in=already_ids)
                        .order_by('-normalized_score')[:total_limit - len(links)]
                    )
                    links.extend(extra)

                return links

            accepted_list = _sample_proportional(
                SpatialTripletScore,
                country_filter & Q(predicted=True) & snapshot_filter,
                limit,
            )
            rejected_list = _sample_proportional(
                SpatialTripletScoreRejected,
                country_filter & snapshot_filter,
                limit,
            )

            total_accepted = SpatialTripletScore.objects.filter(
                country_filter & Q(predicted=True) & snapshot_filter
            ).count()
            total_rejected = SpatialTripletScoreRejected.objects.filter(
                country_filter & snapshot_filter
            ).count()

            # Batch-resolve OsmEntity geometries for head and tail entities
            def _resolve_geoms(links):
                """Resolve (osm_type, osm_id) → (lat, lon) via OsmEntity.geom."""
                ids = set()
                for link in links:
                    ids.add((link.head_osm_type, link.head_osm_id))
                    ids.add((link.tail_osm_type, link.tail_osm_id))
                if not ids:
                    return {}

                # Build a Q filter for all referenced entities
                # Use OR of (osm_type=X, osm_id=Y) pairs
                q_filter = Q()
                for osm_type, osm_id in ids:
                    q_filter |= Q(osm_type=osm_type, osm_id=osm_id)

                geom_map = {}
                for entity in OsmEntity.objects.filter(q_filter).only(
                    'osm_type', 'osm_id', 'geom'
                ):
                    if entity.geom:
                        geom_map[(entity.osm_type, entity.osm_id)] = {
                            'lat': entity.geom.y,
                            'lon': entity.geom.x,
                        }
                return geom_map

            accepted_geom_map = _resolve_geoms(accepted_list)
            rejected_geom_map = _resolve_geoms(rejected_list)

            def _serialize_links(links, geom_map):
                result = []
                for link in links:
                    head_key = (link.head_osm_type, link.head_osm_id)
                    tail_key = (link.tail_osm_type, link.tail_osm_id)
                    head_geom = geom_map.get(head_key)
                    tail_geom = geom_map.get(tail_key)
                    # Skip links where either endpoint has no geometry
                    if not head_geom or not tail_geom:
                        continue
                    result.append({
                        'head': {
                            'lat': head_geom['lat'],
                            'lon': head_geom['lon'],
                            'osm_type': link.head_osm_type,
                            'osm_id': link.head_osm_id,
                        },
                        'tail': {
                            'lat': tail_geom['lat'],
                            'lon': tail_geom['lon'],
                            'osm_type': link.tail_osm_type,
                            'osm_id': link.tail_osm_id,
                        },
                        'relation': link.relation,
                        'normalized_score': round(link.normalized_score, 4),
                    })
                return result

            accepted_serialized = _serialize_links(accepted_list, accepted_geom_map)
            rejected_serialized = _serialize_links(rejected_list, rejected_geom_map)

            return Response({
                'country_name': country_name,
                'iso': iso,
                'accepted': accepted_serialized,
                'rejected': rejected_serialized,
                'total_accepted': total_accepted,
                'total_rejected': total_rejected,
                'returned_accepted': len(accepted_serialized),
                'returned_rejected': len(rejected_serialized),
            })

        except Exception as exc:
            logger.error(
                f"AugmentedLinksGeomView error for {country_name}: {exc}",
                exc_info=True,
            )
            return Response(
                {'error': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
