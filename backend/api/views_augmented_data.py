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
            from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService

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
