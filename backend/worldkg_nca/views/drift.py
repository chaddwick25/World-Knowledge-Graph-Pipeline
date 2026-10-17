from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.core.paginator import Paginator

from api.models import WorldKGClassDrift


@api_view(['GET'])
def worldkg_drift_list(request):
    """
    List WorldKG class drift records.

    GET /api/nca/drift/?region={str}&magnitude={low|medium|high|extreme}

    Returns paginated list of drift records.

    Note: fingerprint/drift computation is pipeline-side (Step 6 fills the
    newest pair; `compute_class_drift` backfills older pairs). The manual
    POST compute endpoints were removed 2026-10-16 — unauthenticated job
    triggers with no callers; re-add behind the admin panel if needed.
    """
    region = request.GET.get('region')
    magnitude = request.GET.get('magnitude')
    page = int(request.GET.get('page', 1))
    page_size = int(request.GET.get('page_size', 20))

    drifts = WorldKGClassDrift.objects.all()

    if region:
        drifts = drifts.filter(region=region)
    if magnitude:
        drifts = drifts.filter(drift_magnitude=magnitude)

    paginator = Paginator(drifts, page_size)
    page_obj = paginator.get_page(page)

    results = [{
        "id": str(d.id),
        "region": d.region,
        "snapshot_from_date": d.snapshot_from.snapshot_date,
        "snapshot_to_date": d.snapshot_to.snapshot_date,
        "js_divergence": d.js_divergence,
        "drift_magnitude": d.drift_magnitude,
        "depth_delta": d.depth_delta
    } for d in page_obj]

    return Response({
        "count": paginator.count,
        "page": page,
        "page_size": page_size,
        "results": results
    })
