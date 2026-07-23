import logging
from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from orchestration.models import ProcessingSession
from extraction.services.osm_wikidata_resolver import get_country_by_name
from extraction.services.regional_path_service import normalize_country_slug, normalize_continent_slug
from extraction.services.subgraph_list_service import build_subgraph_list
from igea.tasks import run_uslp_for_subgraph_batch

logger = logging.getLogger(__name__)

#TODO: looking into this
@api_view(['POST'])
def run_igea_alignment(request):
    """
    POST /api/igea/align/
    
    Run IGEA iterative entity alignment.
    
    Body:
        {
            "country_code": "DE",
            "method": "cosine" | "cross_attention",
            "iterations": 3,
            "threshold": 0.6,
            "dry_run": false
        }
    """
    return Response({"message": "IGEA alignment endpoint - implementation pending"}, status=status.HTTP_501_NOT_IMPLEMENTED)


@api_view(['GET'])
def alignment_status(request):
    """
    GET /api/igea/align/status/
    
    Get alignment statistics and progress.
    """
    return Response({"message": "Alignment status endpoint - implementation pending"}, status=status.HTTP_501_NOT_IMPLEMENTED)


@api_view(['GET'])
def alignment_results(request):
    """
    GET /api/igea/align/results/?osm_id=123&osm_type=node
    
    Query alignment results for specific entities.
    """
    return Response({"message": "Alignment results endpoint - implementation pending"}, status=status.HTTP_501_NOT_IMPLEMENTED)


@api_view(['POST'])
def predict_triplets(request):
    """Trigger USLP spatial link prediction for subgraphs of a country.

    POST /api/igea/triplets/predict/

    Expected body (minimal):
        {
            "country_name": "Tanzania",
            "auto_all": true,
            "subgraphs": [
                {"name": "Mara Region", "slug": "mara_region", "poly_path": "/path/to/poly"},
                ...
            ],
            "threshold": 0.6,
            "top_k": 5,
            "limit": 200000,
            "max_heads": 50000,
            "use_gpu": true,
            "gpu_device": "cuda:0",
            "use_fp64": false,
            "session_id": "<optional existing ProcessingSession UUID>"
        }

    If auto_all is true, the view will derive subgraphs from the filesystem
    under OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/subgraphs.
    """

    country_name = (request.data.get('country_name') or '').strip()
    auto_all = bool(request.data.get('auto_all', True))
    subgraphs_payload = request.data.get('subgraphs') or []

    if not country_name:
        return Response(
            {"error": "country_name is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Build subgraph list using shared service
    try:
        subgraphs = build_subgraph_list(
            country_name=country_name,
            auto_all=auto_all,
            selected=subgraphs_payload if not auto_all else None,
        )
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Resolve country metadata for response (needed for session config)
    country_data = get_country_by_name(country_name)
    if not country_data:
        return Response(
            {"error": f"Could not resolve country metadata for {country_name}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    continent_raw = country_data.get('continent_name') or ''
    continent = normalize_continent_slug(continent_raw) if continent_raw else 'unknown'
    raw_slug = country_data.get('slug') or country_name
    country_slug = normalize_country_slug(raw_slug)

    # USLP parameters (use settings defaults, allow request override)
    threshold = float(request.data.get('threshold', settings.USLP_THRESHOLD))
    top_k = int(request.data.get('top_k', settings.USLP_TOP_K))
    limit = int(request.data.get('limit', settings.USLP_LIMIT))
    max_heads = int(request.data.get('max_heads', settings.USLP_MAX_HEADS))

    # GPU handling: 'auto' means detect, 'true'/'false' means force
    use_gpu_setting = request.data.get('use_gpu', settings.USLP_USE_GPU)
    if use_gpu_setting == 'auto':
        try:
            import torch
            use_gpu = torch.cuda.is_available()
            logger.info(f"GPU auto-detection: CUDA available = {use_gpu}")
        except ImportError:
            use_gpu = False
            logger.warning("PyTorch not available, falling back to CPU")
    else:
        use_gpu = bool(use_gpu_setting)

    gpu_device = request.data.get('gpu_device', settings.USLP_GPU_DEVICE)
    use_fp64 = bool(request.data.get('use_fp64', settings.USLP_USE_FP64))

    # Optional existing ProcessingSession for websocket tracking
    session_id = request.data.get('session_id')
    session = None
    if session_id:
        session = ProcessingSession.objects.filter(id=session_id).first()

    if not session:
        # Try to find an existing pipeline session for this country
        # This ensures websocket updates go to the right frontend connection
        existing_session = ProcessingSession.objects.filter(
            configuration__country_name=country_name,
            session_type__in=[
                ProcessingSession.SessionType.WORLDKG_PIPELINE,
                ProcessingSession.SessionType.TEMPORAL_CORPUS,
            ],
            status__in=[
                ProcessingSession.SessionStatus.IN_PROGRESS,
                ProcessingSession.SessionStatus.PENDING,
            ]
        ).order_by('-created_at').first()

        if existing_session:
            session = existing_session
            session_id = str(session.id)
            logger.info(f"Reusing existing pipeline session {session_id} for USLP")
        else:
            # Create a dedicated session for this USLP run
            session = ProcessingSession.objects.create(
                session_name=f"USLP Subgraphs: {country_name}",
                session_type=ProcessingSession.SessionType.DATASET_GENERATION,
                status=ProcessingSession.SessionStatus.IN_PROGRESS,
                configuration={
                    'country_name': country_name,
                    'continent': continent,
                    'country_slug': country_slug,
                    'auto_all': auto_all,
                    'subgraph_count': len(subgraphs),
                },
            )
            session_id = str(session.id)  # Use the new session_id for websocket

    # Get snapshot_id from session configuration if available
    snapshot_id = None
    if session and session.configuration:
        snapshot_id = session.configuration.get('snapshot_id')

    # Dispatch Celery batch task (or run synchronously if Celery unavailable)
    try:
        async_result = run_uslp_for_subgraph_batch.delay(
            country_name=country_name,
            subgraphs=subgraphs,
            threshold=threshold,
            top_k=top_k,
            limit=limit,
            max_heads=max_heads,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            use_fp64=use_fp64,
            snapshot_id=snapshot_id,
            session_id=str(session.id),
        )
        task_id = async_result.id
        response_status = status.HTTP_202_ACCEPTED
        response_status_text = "started"
    except AttributeError:
        # Celery not available - run synchronously for testing
        logger.warning("Celery not available, running USLP task synchronously")
        summary = run_uslp_for_subgraph_batch(
            country_name=country_name,
            subgraphs=subgraphs,
            threshold=threshold,
            top_k=top_k,
            limit=limit,
            max_heads=max_heads,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            use_fp64=use_fp64,
            snapshot_id=snapshot_id,
            session_id=str(session.id),
        )
        task_id = None
        response_status = status.HTTP_200_OK
        response_status_text = "completed" if summary.get("success") else "failed"

    return Response(
        {
            "status": response_status_text,
            "country_name": country_name,
            "continent": continent,
            "country_slug": country_slug,
            "session_id": str(session.id),
            "task_id": task_id,
            "subgraph_count": len(subgraphs),
            "auto_all": auto_all,
            "threshold": threshold,
            "top_k": top_k,
            "limit": limit,
            "max_heads": max_heads,
            "use_gpu": use_gpu,
            "gpu_device": gpu_device,
            "use_fp64": use_fp64,
        },
        status=response_status,
    )


@api_view(['POST'])
def score_triplet(request):
    """
    POST /api/igea/triplets/score/
    
    Score a specific (head, relation, tail) triplet.
    
    Body:
        {
            "head_osm_id": 123,
            "relation": "isInCounty",
            "tail_osm_id": 456
        }
    
    Returns tri-space scores: geo, name, topo, total
    """
    return Response({"message": "Triplet scoring endpoint - implementation pending"}, status=status.HTTP_501_NOT_IMPLEMENTED)


@api_view(['POST'])
def validate_triple(request):
    """
    POST /api/igea/triplets/validate/
    
    Validate triplet using TransE topological constraints.
    """
    return Response({"message": "Triplet validation endpoint - implementation pending"}, status=status.HTTP_501_NOT_IMPLEMENTED)
