from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.core.paginator import Paginator
from django.db.models import FloatField, Q
from django.db.models.expressions import RawSQL
from django.contrib.gis.geos import Polygon, Point
from django.contrib.gis.db.models.functions import Distance
from django.conf import settings
import math

from worldkg_nca.models import OsmEntity, PrecomputedLinkCandidate
from extraction.models import ProjectionWeightAsset
from semantic_search.services.worldkg_enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service
from api.models import TemporalSnapshot, WorldKGClassDrift
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from extraction.services.osm_wikidata_resolver import resolve_country_bbox, get_country_by_name
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
from extraction.services.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


@api_view(['POST'])
def worldkg_semantic_triplet_search(request):
    """
    Triple-space semantic search over OSM entities for a given country.

    POST /api/nca/semantic-triplet-search/
    Body: {
        "country_code": "Belize",        # Country name or ISO code
        "query_tags": {"amenity": "cafe"},
        "lat": 17.25,                     # Optional: geographic anchor
        "lon": -88.77,                    # Optional: geographic anchor
        "rdf_type": "wkgs:Cafe",          # Optional: WorldKG class filter
        "top_k": 20,                      # 1-100, default 20
        "exact_tag_match": false,         # Optional: require exact tag match (default false)
        "name_distance_threshold": 0.3,  # Optional: max cosine distance for semantic match (default 0.3)
        "use_ann": false                  # Optional: use 400D static_embedding with HNSW (default false)
    }

    Scoring:
        S_name  = 1 - cosine_distance(GV-Tags, query_embedding)
        S_geo   = 1 / (1 + distance_km)  (0.0 when no lat/lon given)
        S_class = 1.0 if entity class matches rdf_type (incl. superclasses)
        S_total = S_name + S_geo + S_class

    Returns:
        {"country_code": str, "top_k": int, "count": int, "results": [...]}
    """
    country_code = request.data.get("country_code")
    query_tags = request.data.get("query_tags") or {}
    natural_query = request.data.get("natural_query") or ""
    lat = request.data.get("lat")
    lon = request.data.get("lon")
    rdf_type = request.data.get("rdf_type")
    top_k = request.data.get("top_k", 20)
    exact_tag_match = request.data.get("exact_tag_match", False)
    # Cosine distance threshold for semantic filtering (0 = identical, 2 = opposite).
    # 0.75 keeps entities with cosine_similarity >= 0.25 — a reasonable cutoff for
    # L2-normalized FastText tag embeddings.  The old default of 0.95 was effectively
    # no filter (similarity >= 0.05), which let class_score dominate ranking.
    name_distance_threshold = request.data.get("name_distance_threshold", 0.75)
    use_ann = request.data.get("use_ann", False)
    use_learned_weights = request.data.get("use_learned_weights", False)
    # snapshot_date filters OsmEntity by snapshot_id (CharField, e.g. "2025_12_31")
    snapshot_date = request.data.get("snapshot_date")
    # Optional: filter within a subdivision (province/state/municipality) by Wikidata QID
    subdivision_qid = request.data.get("subdivision_qid")

    # Auto-infer rdf_type from query_tags if not provided
    if not rdf_type and query_tags:
        ontology_service = get_worldkg_ontology_service()
        inferred_class = ontology_service.predict_class_from_tags(query_tags)
        if inferred_class:
            rdf_type = inferred_class

    # Auto-infer rdf_type from natural_query if not provided
    if not rdf_type and natural_query:
        class_keyword_map = {
            "wkgs:Cafe": ["cafe", "coffee", "coffee shop", "espresso"],
            "wkgs:Restaurant": ["restaurant", "diner", "food", "dining"],
            "wkgs:Hotel": ["hotel", "resort", "lodging"],
            "wkgs:Hospital": ["hospital", "clinic", "medical", "health"],
            "wkgs:School": ["school", "university", "college", "campus"],
            "wkgs:Shop": ["shop", "store", "mall", "market"],
            "wkgs:Amenity": ["amenity", "amenities", "place", "places"],
        }
        query_lower = natural_query.lower()
        for cls, keywords in class_keyword_map.items():
            if any(kw in query_lower for kw in keywords):
                rdf_type = cls
                break

        # Convert natural language to structured query_tags for better matching
        # Extract class keywords and convert to OSM tags
        tag_mapping = {
            "school": "amenity",
            "cafe": "amenity",
            "restaurant": "amenity",
            "hotel": "tourism",
            "hospital": "amenity",
            "shop": "shop",
        }
        for keyword, osm_key in tag_mapping.items():
            if keyword in query_lower and not query_tags:
                query_tags = {osm_key: keyword}
                break
    
    # Adjust threshold for ANN mode (400D distances are different from 300D)
    if use_ann:
        name_distance_threshold = request.data.get("name_distance_threshold", 1.0)

    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 20

    if not country_code and not subdivision_qid:
        return Response(
            {"error": "country_code or subdivision_qid required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not query_tags and not natural_query:
        return Response(
            {"error": "Either query_tags or natural_query is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def _safe_float(value: float, default: float = 0.0) -> float:
        """Convert value to a finite float suitable for JSON.

        Any NaN/inf or conversion error is mapped to the provided default.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return v

    # The frontend sends a full country name (e.g. "Belize"), not an ISO code.
    # Use the same bbox resolution logic as enrichment service for consistency,
    # but be robust to callers that pass slug-style identifiers such as
    # "ireland-and-northern-ireland" or "Ireland_and_northern_ireland".
    from extraction.services.country_override_service import get_country_override_record
    from extraction.models import OsmBoundary
    from semantic_search.utils.subdivision_resolver import (
        resolve_subdivision_bbox as _resolve_subdivision_bbox,
        resolve_subdivision_country_code,
    )

    # If subdivision_qid is provided, use the subdivision bbox directly and
    # infer the country_code if not explicitly given.
    if subdivision_qid:
        bbox = _resolve_subdivision_bbox(subdivision_qid)
        if not bbox:
            return Response(
                {"error": f"Could not resolve subdivision_qid={subdivision_qid}. "
                          "Ensure SubgraphProfile is populated with bbox for this QID."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Infer country_code from the subdivision if not provided
        if not country_code:
            inferred_iso = resolve_subdivision_country_code(subdivision_qid)
            if inferred_iso:
                country_code = inferred_iso
    else:
        # Prefer OsmBoundary bbox (same as enrichment service).
        # Be robust to slug-style identifiers like "ireland_and_northern_ireland".
        human_name = (
            str(country_code).replace("_", " ").replace("-", " ").strip()
            if country_code
            else ""
        )
        boundary_qs = OsmBoundary.objects.filter(admin_level=2)
        if human_name:
            boundary = (
                boundary_qs.filter(
                    Q(name__icontains=country_code)
                    | Q(name_en__icontains=country_code)
                    | Q(name__icontains=human_name)
                    | Q(name_en__icontains=human_name)
                )
                .first()
            )
        else:
            boundary = boundary_qs.filter(name__icontains=country_code).first()
        if boundary and boundary.bbox:
            bbox = tuple(boundary.bbox)
        else:
            # Fallback to ISO/QID and override-aware resolution.
            # First, try robust ISO resolution via resolve_iso_code(),
            # which already understands slugs and Wikidata IDs.
            iso_code = None
            resolved_iso = None
            try:
                resolved_iso = resolve_iso_code(country_code)
            except Exception:
                resolved_iso = None

            if resolved_iso:
                iso_code = resolved_iso
            else:
                # Fallback to country override service when the caller passes an
                # ISO/QID directly.
                override_record = get_country_override_record(country_code)
                if override_record:
                    # Use override to get the canonical ISO code if available
                    iso_code = override_record.get("iso_code") or country_code.upper()
                else:
                    # Try direct ISO/QID resolution
                    iso_code = country_code.upper()

            bbox = resolve_country_bbox(iso_code, None)
            if not bbox:
                # Fallback to name-based lookup via OSMWikiDataHierarchy
                country_meta = get_country_by_name(country_code)
                if country_meta:
                    iso = (
                        country_meta.get("wikidata_id")
                        or (country_meta.get("wkg_uri", "") or "").rsplit("/", 1)[-1]
                    )
                    if iso:
                        bbox = resolve_country_bbox(iso, None)

    if not bbox:
        return Response(
            {"error": f"Could not resolve bbox for country_code={country_code}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    min_lon, min_lat, max_lon, max_lat = bbox
    polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))

    qs = OsmEntity.objects.using("vectors").filter(
        geom__within=polygon,
        gv_tags_embedding__isnull=False,
    )

    # Filter by snapshot_date when provided (OsmEntity.snapshot_id is a CharField)
    if snapshot_date:
        qs = qs.filter(snapshot_id=snapshot_date)

    # Filter by inferred or provided rdf_type (when auto-inferred, filter to ensure relevance)
    auto_inferred_rdf_type = rdf_type and not request.data.get("rdf_type")
    # Don't filter at database level for natural queries - use post-scoring instead
    # if auto_inferred_rdf_type:
    #     qs = qs.filter(wkg_class=rdf_type)

    # Exact tag matching filter
    if exact_tag_match and query_tags:
        tag_filters = Q()
        for key, value in query_tags.items():
            if value:
                tag_filters &= Q(**{f"tags__{key}": value})
            else:
                tag_filters &= Q(tags__has_key=key)
        qs = qs.filter(tag_filters)

    # Optional: load learned projection weights for this country
    w_geo = 1.0
    w_name = 1.0
    w_class = 1.0

    # Boost class score when rdf_type is auto-inferred from query
    if rdf_type and not request.data.get("rdf_type"):
        w_class = 5.0  # Much higher weight for auto-inferred class matching

    if use_learned_weights:
        try:
            weight_asset = (
                ProjectionWeightAsset.objects.filter(
                    Q(country_code__iexact=country_code) | Q(region_name__iexact=country_code),
                    status=ProjectionWeightAsset.Status.COMPLETED,
                )
                .order_by("-updated_at")
                .first()
            )
        except Exception:
            weight_asset = None

        if weight_asset:
            w_geo = float(weight_asset.w_geo)
            w_name = float(weight_asset.w_name)
            w_class = float(weight_asset.w_class)
        else:
            # Fallback to default weights when none are available
            use_learned_weights = False

    # ANN Search Path (400D static_embedding)
    if use_ann:
        # Check if static_embedding is available
        qs = qs.filter(static_embedding__isnull=False)

        # Generate 300D query embedding from tags
        tag_counts = FastTextEmbeddingService.build_tag_counts_from_osm_tags(query_tags)
        query_embedding_300d = FastTextEmbeddingService.calculate_embedding(tag_counts)
        query_list_300d = query_embedding_300d.tolist()

        # Pad to 400D (100D spatial component = zeros for now)
        # TODO: Future enhancement: generate spatial component from lat/lon
        query_list_400d = query_list_300d + [0.0] * 100

        # Use HNSW index for fast ANN search
        qs = qs.annotate(
            ann_distance=RawSQL(
                "static_embedding <=> %s::vector",
                (query_list_400d,),
                output_field=FloatField(),
            )
        )

        # Add geo distance annotation if lat/lon provided
        point = None
        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                point = Point(lon_f, lat_f, srid=4326)
                qs = qs.annotate(geo_distance=Distance("geom", point))
            except (TypeError, ValueError):
                point = None

        # Get top candidates via ANN (no re-ranking for speed)
        initial_limit = max(top_k * 10, top_k)
        candidates = list(qs.order_by("ann_distance")[:initial_limit])

        # Direct scoring from ANN distance, with optional learned weights
        results = []
        for entity in candidates:
            ann_distance = getattr(entity, "ann_distance", None)
            try:
                ann_distance = float(ann_distance) if ann_distance is not None else 1.0
            except (TypeError, ValueError):
                ann_distance = 1.0

            # Convert ANN distance to similarity score
            name_score = 1.0 - ann_distance

            # Apply semantic distance threshold
            if ann_distance > name_distance_threshold:
                continue

            # Geographic scoring
            geo_score = 0.0
            if point is not None:
                geo_value = getattr(entity, "geo_distance", None)
                if geo_value is not None:
                    try:
                        if hasattr(geo_value, "m"):
                            dist_m = float(geo_value.m)
                        else:
                            dist_m = float(geo_value)
                        dist_km = dist_m / 1000.0
                        geo_score = 1.0 / (1.0 + dist_km)
                    except (TypeError, ValueError):
                        geo_score = 0.0

            # Class scoring
            class_score = 0.0
            if rdf_type:
                if entity.wkg_class == rdf_type or (
                    entity.wkg_superclasses and rdf_type in entity.wkg_superclasses
                ):
                    class_score = 1.0

            if use_learned_weights:
                final_score = (
                    (w_name * name_score)
                    + (w_geo * geo_score)
                    + (w_class * class_score)
                )
            else:
                final_score = name_score + geo_score + class_score

            results.append(
                {
                    "osm_type": entity.osm_type,
                    "osm_id": entity.osm_id,
                    "tags": entity.tags,
                    "wkg_class": entity.wkg_class,
                    "wkg_superclasses": entity.wkg_superclasses,
                    "geom": {
                        "lat": entity.geom.y if entity.geom else None,
                        "lon": entity.geom.x if entity.geom else None,
                    }
                    if entity.geom
                    else None,
                    "scores": {
                        "name_score": _safe_float(name_score),
                        "geo_score": _safe_float(geo_score),
                        "class_score": _safe_float(class_score),
                        "final_score": _safe_float(final_score),
                        "ann_distance": _safe_float(ann_distance),
                    },
                }
            )

        results.sort(key=lambda r: r["scores"]["final_score"], reverse=True)
        results = results[:top_k]

        return Response(
            {
                "country_code": country_code,
                "top_k": top_k,
                "count": len(results),
                "results": results,
                "search_mode": "ann",
            }
        )

    # Triple-Space Search Path (current implementation)
    # Support both structured tag queries and natural language queries.
    # When query_tags were extracted from a natural query (lines 549-562), use
    # the clean tag-based embedding — the raw text embedding dilutes the signal
    # with stop words and country names (e.g. "find cafes in belize" embeds
    # "find", "in", "belize" as noise). Fall back to text embedding only when
    # no tags were extracted.
    if natural_query and not query_tags:
        query_embedding = FastTextEmbeddingService.calculate_text_embedding(natural_query)
    else:
        tag_counts = FastTextEmbeddingService.build_tag_counts_from_osm_tags(query_tags)
        query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
    query_list = query_embedding.tolist()

    # Use exact cosine distance (not HNSW approximation).  The + 0 makes the
    # expression non-indexable, preventing PostgreSQL from using the HNSW
    # index on gv_tags_embedding — which returns approximate results that
    # miss relevant entities (e.g. cafe query returns highway=service at
    # dist=0.62 instead of actual cafes at dist=0.16).
    qs = qs.annotate(
        name_distance=RawSQL(
            "(gv_tags_embedding <=> %s::vector) + 0",
            (query_list,),
            output_field=FloatField(),
        )
    )

    point = None
    if lat is not None and lon is not None:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            point = Point(lon_f, lat_f, srid=4326)
        except (TypeError, ValueError):
            point = None

    if point is not None:
        qs = qs.annotate(geo_distance=Distance("geom", point))

    # Increase candidate limit when rdf_type is auto-inferred to find class matches
    initial_limit = max(top_k * 50, top_k) if auto_inferred_rdf_type else max(top_k * 10, top_k)
    candidates = list(qs.order_by("name_distance")[:initial_limit])

    results = []
    for entity in candidates:
        name_distance = getattr(entity, "name_distance", None)
        try:
            name_distance = float(name_distance) if name_distance is not None else 1.0
        except (TypeError, ValueError):
            name_distance = 1.0

        # Apply semantic distance threshold
        if name_distance > name_distance_threshold:
            continue

        name_score = 1.0 - name_distance

        geo_score = 0.0
        geo_value = getattr(entity, "geo_distance", None)
        if geo_value is not None:
            try:
                if hasattr(geo_value, "m"):
                    dist_m = float(geo_value.m)
                else:
                    dist_m = float(geo_value)
                dist_km = dist_m / 1000.0
                geo_score = 1.0 / (1.0 + dist_km)
            except (TypeError, ValueError):
                geo_score = 0.0

        class_score = 0.0
        if rdf_type:
            if entity.wkg_class == rdf_type or (
                entity.wkg_superclasses and rdf_type in entity.wkg_superclasses
            ):
                class_score = 1.0

        if use_learned_weights:
            final_score = (
                (w_name * name_score)
                + (w_geo * geo_score)
                + (w_class * class_score)
            )
        else:
            final_score = name_score + geo_score + class_score

        results.append(
            {
                "osm_type": entity.osm_type,
                "osm_id": entity.osm_id,
                "tags": entity.tags,
                "wkg_class": entity.wkg_class,
                "wkg_superclasses": entity.wkg_superclasses,
                "geom": {
                    "lat": entity.geom.y if entity.geom else None,
                    "lon": entity.geom.x if entity.geom else None,
                }
                if entity.geom
                else None,
                "scores": {
                    "name_score": _safe_float(name_score),
                    "geo_score": _safe_float(geo_score),
                    "class_score": _safe_float(class_score),
                    "final_score": _safe_float(final_score),
                },
            }
        )

    results.sort(key=lambda r: r["scores"]["final_score"], reverse=True)

    # When rdf_type is auto-inferred, boost class-matching entities in ranking
    if auto_inferred_rdf_type:
        class_matches = [r for r in results if r["scores"]["class_score"] == 1.0]
        if class_matches:
            # Boost class matches by adding 2.0 to their final score
            for r in class_matches:
                r["scores"]["final_score"] += 2.0
            # Re-sort with boosted scores
            results.sort(key=lambda r: r["scores"]["final_score"], reverse=True)

    results = results[:top_k]

    response_payload = {
        "country_code": country_code,
        "top_k": top_k,
        "count": len(results),
        "results": results,
        "search_mode": "triple_space",
    }
    if use_learned_weights:
        response_payload["projection_weights"] = {
            "w_geo": w_geo,
            "w_name": w_name,
            "w_class": w_class,
            "source": "learned",
        }
    return Response(response_payload)


@api_view(["POST"])
def worldkg_semantic_query_plan(request):
    """Semantic query planner for WorldKG / NCA.

    This endpoint converts a free-text query into a structured plan that can be
    executed by worldkg_semantic_triplet_search. It does not call any external
    LLM APIs; instead it uses lightweight keyword heuristics and existing
    country metadata.
    """
    country_code = request.data.get("country_code")
    query_text = request.data.get("query") or ""
    top_k = request.data.get("top_k", 20)
    use_learned_weights = bool(request.data.get("use_learned_weights", True))
    use_ann = bool(request.data.get("use_ann", False))

    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 20

    if not country_code:
        return Response({"error": "country_code required"}, status=status.HTTP_400_BAD_REQUEST)

    if not query_text.strip():
        return Response({"error": "query required"}, status=status.HTTP_400_BAD_REQUEST)

    # Resolve country bbox using same logic as semantic_triplet_search
    bbox = resolve_country_bbox(country_code, None)
    normalized_country = country_code
    if not bbox:
        country_meta = get_country_by_name(country_code)
        if country_meta:
            iso = (
                country_meta.get("wikidata_id")
                or (country_meta.get("wkg_uri", "") or "").rsplit("/", 1)[-1]
            )
            if iso:
                normalized_country = iso
                bbox = resolve_country_bbox(iso, None)

    if not bbox:
        return Response(
            {"error": f"Could not resolve bbox for country_code={country_code}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    query_lower = query_text.lower()

    class_keyword_map = {
        "wkgs:Cafe": ["cafe", "coffee", "coffee shop", "espresso"],
        "wkgs:Restaurant": ["restaurant", "diner", "food", "dining"],
        "wkgs:Hotel": ["hotel", "resort", "lodging"],
        "wkgs:Hospital": ["hospital", "clinic", "medical", "health"],
        "wkgs:School": ["school", "university", "college", "campus"],
        "wkgs:Shop": ["shop", "store", "mall", "market"],
        "wkgs:Amenity": ["amenity", "amenities", "place", "places"],
    }

    rdf_types = []
    for cls, keywords in class_keyword_map.items():
        if any(kw in query_lower for kw in keywords):
            rdf_types.append(cls)

    primary_rdf_type = rdf_types[0] if rdf_types else None

    # Simple region hints from capitalized tokens (potential city/area names)
    region_hints = []
    for token in query_text.split():
        stripped = token.strip(",.()")
        if len(stripped) > 1 and stripped[0].isupper():
            region_hints.append(stripped)

    min_lon, min_lat, max_lon, max_lat = bbox

    semantic_triplet_params = {
        "country_code": country_code,
        "natural_query": query_text,
        "top_k": top_k,
        "use_learned_weights": use_learned_weights,
    }
    if use_ann:
        semantic_triplet_params["use_ann"] = True
    if primary_rdf_type:
        semantic_triplet_params["rdf_type"] = primary_rdf_type

    plan = {
        "country_code": country_code,
        "normalized_country": normalized_country,
        "query": query_text,
        "top_k": top_k,
        "country_bbox": {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
        "rdf_types": rdf_types,
        "primary_rdf_type": primary_rdf_type,
        "use_learned_weights": use_learned_weights,
        "use_ann": use_ann,
        "region_hints": region_hints,
        "executor": "worldkg_semantic_triplet_search",
        "semantic_triplet_params": semantic_triplet_params,
    }

    return Response(plan)


@api_view(['GET'])
def worldkg_subdivisions(request):
    """List available subdivisions (provinces/states/municipalities) for a country.

    GET /api/nca/subdivisions/?country_code=NI

    Returns subdivisions from ``SubgraphProfile`` that have a Wikidata QID and
    bbox populated, so the frontend can present a subdivision picker and pass
    ``subdivision_qid`` to the search endpoints.

    Query params:
        country_code: ISO-2 code (e.g. "NI") or country name.  Required.

    Response:
        {
            "country_code": "NI",
            "count": 15,
            "subdivisions": [
                {
                    "wikidata_id": "Q260009",
                    "name": "Managua",
                    "slug": "managua",
                    "admin_level": null,
                    "osm_relation_id": 2194897,
                    "bbox": [-86.95, 11.98, -86.16, 12.62]
                },
                ...
            ]
        }
    """
    from orchestration.models import SubgraphProfile, CountryPipelineProfile

    country_code = request.query_params.get('country_code')
    if not country_code:
        return Response(
            {"error": "country_code query parameter required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Resolve to CountryPipelineProfile
    profile = (
        CountryPipelineProfile.objects.filter(iso2__iexact=country_code).first()
        or CountryPipelineProfile.objects.filter(
            canonical_name__icontains=country_code.replace('_', ' ').replace('-', ' ')
        ).first()
    )
    if not profile:
        return Response(
            {"error": f"CountryPipelineProfile not found for {country_code}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    subdivisions = []
    qs = SubgraphProfile.objects.filter(
        country_profile=profile,
        wikidata_id__isnull=False,
    ).exclude(wikidata_id='').order_by('name')

    for sg in qs:
        has_bbox = all(v is not None for v in (
            sg.bbox_min_lon, sg.bbox_min_lat, sg.bbox_max_lon, sg.bbox_max_lat,
        ))
        subdivisions.append({
            "wikidata_id": sg.wikidata_id,
            "name": sg.name,
            "slug": sg.slug,
            "admin_level": sg.admin_level,
            "osm_relation_id": sg.osm_relation_id,
            "bbox": (
                [sg.bbox_min_lon, sg.bbox_min_lat, sg.bbox_max_lon, sg.bbox_max_lat]
                if has_bbox else None
            ),
        })

    return Response({
        "country_code": profile.iso2 or country_code,
        "count": len(subdivisions),
        "subdivisions": subdivisions,
    })
