import json
import queue
import threading

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET
from django.core.paginator import Paginator
from django.db.models import FloatField, Q, Value, F
from django.db.models.expressions import RawSQL, Func
from django.contrib.gis.geos import Polygon, Point
from django.contrib.gis.db.models.functions import Distance
from django.conf import settings
import math
import re
import unicodedata

from worldkg_nca.models import OsmEntity, PrecomputedLinkCandidate
from core.models import ProjectionWeightAsset
from semantic_search.services.romanizing_names.registry import RomanizerRegistry
from semantic_search.services.worldkg_enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service
from api.models import WorldKGClassDrift
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from core.services.planet_init.osm_wikidata_resolver import resolve_country_bbox, get_country_by_name
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


# TODO: Refactor this monolithic file

# ── Name-search noise filtering helpers ──────────────────────────────────
# OSM tag keys that assert an entity's type/identity.  Entities lacking ALL
# of these keys are treated as noise in name-based search (e.g. a node with
# only {"name": "벤치"} — a bench whose "name" is literally "bench", or
# traffic-sign text leaked into name=).  Configurable via
# SPATIAL_SEMANTICS_CONFIG['type_asserting_keys'].
_TYPE_ASSERTING_KEYS = getattr(
    settings.SPATIAL_SEMANTICS_CONFIG,
    'get',
    lambda *_: None,
)('type_asserting_keys', [
    'amenity', 'shop', 'tourism', 'place', 'highway', 'building',
    'office', 'leisure', 'natural', 'landuse', 'railway', 'aeroway',
    'waterway', 'boundary', 'historic', 'military', 'man_made',
    'public_transport', 'route', 'craft', 'healthcare', 'education',
    'addr:housenumber', 'addr:street', 'contact:phone', 'ref',
]) or [
    'amenity', 'shop', 'tourism', 'place', 'highway', 'building',
    'office', 'leisure', 'natural', 'landuse', 'railway', 'aeroway',
    'waterway', 'boundary', 'historic', 'military', 'man_made',
    'public_transport', 'route', 'craft', 'healthcare', 'education',
    'addr:housenumber', 'addr:street', 'contact:phone', 'ref',
]

_DEFAULT_NAME_DISTANCE_THRESHOLD = settings.SPATIAL_SEMANTICS_CONFIG.get(
    'name_distance_threshold_default', 0.5,
)
_NAME_SUBSTRING_BOOST = settings.SPATIAL_SEMANTICS_CONFIG.get(
    'name_substring_boost', 1.0,
)

# ── Name romanization helpers (delegated to RomanizerRegistry) ──────────
# The romanization logic now lives in semantic_search/services/romanizing_names/.
# These thin wrappers maintain backward compatibility with the search view's
# existing code while the migration to SQL similarity() is in progress.

def _contains_hangul(text: str) -> bool:
    """Return True if *text* contains any Hangul (Korean) characters."""
    from semantic_search.services.romanizing_names.hangul_romanizer import HangulRomanizer
    return HangulRomanizer.detect(text)


def _hangul_to_roman(text: str) -> str:
    """Romanize Hangul text. Delegates to HangulRomanizer."""
    from semantic_search.services.romanizing_names.hangul_romanizer import HangulRomanizer
    return HangulRomanizer.romanize(text)


def _char_ngram_jaccard(s1: str, s2: str, n: int = 2) -> float:
    """Character n-gram Jaccard similarity between two strings.

    Returns a float in [0, 1].  Used as a fallback when
    FastText (cc.en.300) cannot distinguish CJK/Hangul tokens — all OOV
    Korean text collapses to similar subword vectors, so the embedding
    distance is meaningless for Korean name matching.
    """
    if not s1 or not s2:
        return 0.0
    def ngrams(s):
        s = s.lower().replace(' ', '')
        return {s[i:i+n] for i in range(len(s) - n + 1)} if len(s) >= n else {s}
    g1, g2 = ngrams(s1), ngrams(s2)
    if not g1 or not g2:
        return 0.0
    return len(g1 & g2) / len(g1 | g2)


def _cross_script_name_score(query_text: str, entity_name: str) -> float:
    """Cross-script name similarity score for Korean↔Latin matching.

    Uses the RomanizerRegistry to romanize both sides, then computes
    n-gram Jaccard similarity. When name_romanized is populated in the
    DB, this will be replaced by SQL similarity() — but for now this
    maintains the existing behavior for entities not yet romanized.
    """
    if not query_text or not entity_name:
        return 0.0
    q_hangul = _contains_hangul(query_text)
    e_hangul = _contains_hangul(entity_name)
    if q_hangul and e_hangul:
        # Same-script Korean — use n-gram directly
        return _char_ngram_jaccard(query_text, entity_name, n=2)
    if not q_hangul and not e_hangul:
        # Same-script Latin — FastText handles this
        return 0.0
    # Cross-script: romanize the Hangul side via the registry
    if e_hangul:
        romanized = RomanizerRegistry.auto_romanize(entity_name)
        return _char_ngram_jaccard(query_text.lower(), romanized, n=2)
    else:
        romanized = RomanizerRegistry.auto_romanize(query_text)
        return _char_ngram_jaccard(romanized, entity_name.lower(), n=2)


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
        S_xscript = cross-script n-gram similarity (Korean↔Latin fallback)
        S_name_boost = substring match boost (query term in entity name=)
        S_total = S_name + S_geo + S_class + S_xscript + S_name_boost + tag_match

    Noise filtering:
        Entities without any type-asserting OSM key (amenity, shop, tourism,
        place, highway, building, etc.) are excluded from name-based search.
        This eliminates mis-tagged noise (benches named "벤치", traffic-sign
        text in name=, generic nouns).  Set filter_noise=false to disable.

    Returns:
        {"country_code": str, "top_k": int, "count": int, "results": [...]}
    """
    country_code = request.data.get("country_code")
    query_tags = request.data.get("query_tags") or {}
    natural_query = request.data.get("natural_query") or ""
    # Derive a name search term from natural_query OR query_tags["name"].
    # When query_tags contains a "name" key (e.g. {"name": "파리바게뜨"}), treat
    # it as a name search term for cross-script scoring, substring boost, and
    # the Korean ILIKE pre-filter — the same as a natural_query.  Without this,
    # a Korean name in query_tags is fed to FastText cc.en.300 which can't
    # tokenize Hangul, producing meaningless OOV-collapse distances.
    name_search_term = natural_query
    if not name_search_term and isinstance(query_tags, dict):
        name_search_term = (query_tags.get("name") or "").strip()
    lat = request.data.get("lat")
    lon = request.data.get("lon")
    rdf_type = request.data.get("rdf_type")
    top_k = request.data.get("top_k", 20)
    exact_tag_match = request.data.get("exact_tag_match", False)
    # Cosine distance threshold for semantic filtering (0 = identical, 2 = opposite).
    # Default 0.5 (configurable via SPATIAL_SEMANTICS_CONFIG).  The old default
    # of 0.75 was too permissive — FastText OOV-collapse noise (Korean text
    # that cc.en.300 can't tokenize) clustered at ~0.09 distance and passed
    # the filter.  0.5 is a tighter cutoff that still keeps genuine matches.
    name_distance_threshold = request.data.get(
        "name_distance_threshold", _DEFAULT_NAME_DISTANCE_THRESHOLD,
    )
    use_ann = request.data.get("use_ann", False)
    use_learned_weights = request.data.get("use_learned_weights", False)
    # snapshot_date filters OsmEntity by snapshot_id (CharField, e.g. "2025_12_31")
    snapshot_date = request.data.get("snapshot_date")
    # Optional: filter within a subdivision (province/state/municipality) by Wikidata QID
    subdivision_qid = request.data.get("subdivision_qid")
    # Noise filtering: exclude entities without any type-asserting OSM key
    # (eliminates benches named "벤치", traffic-sign text, generic nouns).
    filter_noise = request.data.get("filter_noise", True)

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
    from core.services.snapshot.country_override_service import get_country_override_record
    from core.models import OsmBoundary
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

    # ── Noise filtering ────────────────────────────────────────────────
    # Exclude entities without any type-asserting OSM key.  This eliminates
    # mis-tagged noise: benches whose name= is literally "벤치" (bench),
    # traffic-sign text leaked into name=, generic nouns like "도로" (road),
    # etc.  Uses the GIN index on tags (jsonb ?| operator) for performance.
    # Set filter_noise=false in the request body to disable.
    if filter_noise and _TYPE_ASSERTING_KEYS:
        qs = qs.filter(tags__has_any_keys=_TYPE_ASSERTING_KEYS)

    # Filter by snapshot_date when provided (OsmEntity.snapshot_id is a CharField)
    if snapshot_date:
        qs = qs.filter(snapshot_id=snapshot_date)

    # Filter by inferred or provided rdf_type (when auto-inferred, filter to ensure relevance)
    auto_inferred_rdf_type = rdf_type and not request.data.get("rdf_type")
    # Don't filter at database level for natural queries - use post-scoring instead
    # if auto_inferred_rdf_type:
    #     qs = qs.filter(wkg_class=rdf_type)

    # Tag filtering — always apply when query_tags has specific keys.
    # When exact_tag_match=True: require exact key=value match.
    # When exact_tag_match=False: require the tag key to be present (semantic
    #   ranking handles value similarity). This prevents entities without the
    #   tag at all from polluting results (e.g. querying {"cuisine": "jamaican"}
    #   should not return entities with no cuisine tag).
    if query_tags:
        tag_filters = Q()
        for key, value in query_tags.items():
            if value and exact_tag_match:
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

        # Prepare query name terms for substring matching (same as triple-space)
        _ann_query_name_terms = []
        if name_search_term:
            _ann_query_name_terms = [
                t for t in re.findall(r"[^\s,.;:!?()]+", name_search_term.lower())
                if len(t) >= 3 and t not in FastTextEmbeddingService._STOP_WORDS
            ]

        # Direct scoring from ANN distance, with optional learned weights
        results = []
        for entity in candidates:
            ann_distance = getattr(entity, "ann_distance", None)
            try:
                ann_distance = float(ann_distance) if ann_distance is not None else 1.0
            except (TypeError, ValueError):
                ann_distance = 1.0
            # Guard against NaN (same as triple-space path)
            if not math.isfinite(ann_distance):
                ann_distance = 1.0

            # Cross-script name score (Korean↔Latin fallback) — computed
            # before the threshold check so cross-script matches can bypass
            # the (meaningless for CJK) FastText distance filter.
            xscript_score = 0.0
            entity_name = (entity.tags or {}).get("name", "") or ""
            if name_search_term and entity_name:
                xscript_score = _cross_script_name_score(name_search_term, entity_name)

            # Name substring match boost
            name_boost_score = 0.0
            if _ann_query_name_terms and entity_name:
                name_lower = entity_name.lower()
                for term in _ann_query_name_terms:
                    if term in name_lower:
                        name_boost_score += _NAME_SUBSTRING_BOOST

            # Convert ANN distance to similarity score
            name_score = 1.0 - ann_distance

            # Apply semantic distance threshold, but allow cross-script or
            # name-substring matches to bypass it (same logic as triple-space).
            if ann_distance > name_distance_threshold:
                if xscript_score <= 0.0 and name_boost_score <= 0.0:
                    continue

            # USLP geographic score (geohash P4 cluster centers, d_max=39km)
            geo_score = 0.0
            if point is not None:
                geo_value = getattr(entity, "geo_distance", None)
                if geo_value is not None:
                    try:
                        if hasattr(geo_value, "m"):
                            dist_m = float(geo_value.m)
                        else:
                            dist_m = float(geo_value)
                        import geohash2
                        gh_h = geohash2.encode(point.y, point.x, precision=4)
                        gh_t = geohash2.encode(entity.geom.y, entity.geom.x, precision=4)
                        lat_h, lon_h = geohash2.decode(gh_h)
                        lat_t, lon_t = geohash2.decode(gh_t)
                        cluster_dist_km = math.sqrt(
                            (float(lat_h) - float(lat_t))**2 +
                            (float(lon_h) - float(lon_t))**2
                        ) * 111.0
                        d_max = 39.0
                        geo_score = max(0.0, min(1.0, 1.0 - (cluster_dist_km / d_max)))
                    except (TypeError, ValueError, Exception):
                        geo_score = 0.0

            # Class scoring
            class_score = 0.0
            if rdf_type:
                if entity.wkg_class == rdf_type or (
                    entity.wkg_superclasses and rdf_type in entity.wkg_superclasses
                ):
                    class_score = 1.0

            # Tag match boost (same logic as triple-space path)
            tag_match_score = 0.0
            if query_tags:
                for qk, qv in query_tags.items():
                    if qv and entity.tags.get(qk) == qv:
                        tag_match_score += 1.0

            if use_learned_weights:
                final_score = (
                    (w_name * name_score)
                    + (w_geo * geo_score)
                    + (w_class * class_score)
                    + tag_match_score
                    + xscript_score
                    + name_boost_score
                )
            else:
                final_score = (
                    name_score + geo_score + class_score + tag_match_score
                    + xscript_score + name_boost_score
                )

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
                        "tag_match_score": _safe_float(tag_match_score),
                        "xscript_score": _safe_float(xscript_score),
                        "name_boost_score": _safe_float(name_boost_score),
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
        # Build tag counts for FastText embedding, excluding the "name" key.
        # The name value is handled by name_search_term (cross-script scoring,
        # substring boost, ILIKE pre-filter) — including it in tag_counts would
        # feed Korean text to FastText cc.en.300 which can't tokenize Hangul,
        # producing meaningless OOV-collapse vectors.
        tags_for_embedding = {
            k: v for k, v in query_tags.items() if k != "name"
        } if query_tags else {}
        if tags_for_embedding:
            tag_counts = FastTextEmbeddingService.build_tag_counts_from_osm_tags(tags_for_embedding)
            query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
        elif name_search_term:
            # Only a name was provided (no other tags) — use text embedding
            query_embedding = FastTextEmbeddingService.calculate_text_embedding(name_search_term)
        else:
            query_embedding = FastTextEmbeddingService.calculate_embedding({})
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

    # ── Prepare query terms for name substring matching ────────────────
    # Extract meaningful tokens from natural_query for substring matching
    # against entity name= tags.  This provides a complementary signal to
    # FastText embeddings, especially for cross-script (Korean↔Latin) and
    # misspelling cases where the embedding alone can't distinguish tokens.
    _query_name_terms = []
    if name_search_term:
        _query_name_terms = [
            t for t in re.findall(r"[^\s,.;:!?()]+", name_search_term.lower())
            if len(t) >= 3 and t not in FastTextEmbeddingService._STOP_WORDS
        ]

    # Increase candidate limit when rdf_type is auto-inferred to find class matches
    initial_limit = max(top_k * 50, top_k) if auto_inferred_rdf_type else max(top_k * 10, top_k)

    # ── Korean query handling ──────────────────────────────────────────
    # When the query contains Hangul, FastText cc.en.300 cannot meaningfully
    # rank candidates (all Korean OOV tokens produce similar/NaN distances).
    # In this case, use a Postgres ILIKE pre-filter on the name= tag to find
    # entities whose name contains the query text, then score them with the
    # n-gram similarity.  This is a DB-level fallback for same-script Korean
    # matching that the embedding-based path cannot handle.
    _is_korean_query = _contains_hangul(name_search_term)
    if _is_korean_query and name_search_term:
        # Use ILIKE to find entities with the query text in their name
        # This leverages the GIN index on tags jsonb for fast filtering
        korean_candidates = list(
            qs.filter(tags__name__icontains=name_search_term.strip())
            .order_by("name_distance")[:max(top_k * 10, 100)]
        )
        # Merge with regular candidates (deduplicated by osm_type+osm_id)
        regular_candidates = list(qs.order_by("name_distance")[:initial_limit])
        seen_ids = set()
        candidates = []
        for e in korean_candidates + regular_candidates:
            key = (e.osm_type, e.osm_id)
            if key not in seen_ids:
                seen_ids.add(key)
                candidates.append(e)
    else:
        candidates = list(qs.order_by("name_distance")[:initial_limit])

    results = []
    for entity in candidates:
        name_distance = getattr(entity, "name_distance", None)
        try:
            name_distance = float(name_distance) if name_distance is not None else 1.0
        except (TypeError, ValueError):
            name_distance = 1.0
        # Guard against NaN — FastText cc.en.300 produces NaN distances for
        # Korean-only queries (OOV Hangul tokens yield zero-norm vectors).
        # NaN comparisons are always False, so NaN would bypass the threshold
        # check below.  Treat NaN as max distance (1.0).
        if not math.isfinite(name_distance):
            name_distance = 1.0

        # ── Cross-script name score (Korean↔Latin fallback) ────────────
        # FastText cc.en.300 cannot meaningfully compare Korean and Latin
        # text — all Hangul OOV tokens collapse to similar subword vectors.
        # When the query and entity name use different scripts, compute a
        # character n-gram Jaccard similarity as a complementary signal.
        # NOTE: computed BEFORE the distance threshold check so that cross-
        # script matches can bypass the (meaningless for CJK) FastText
        # distance filter.
        xscript_score = 0.0
        entity_name = (entity.tags or {}).get("name", "") or ""
        if name_search_term and entity_name:
            xscript_score = _cross_script_name_score(name_search_term, entity_name)

        # ── Name substring match boost ─────────────────────────────────
        # When a query term appears as a substring of the entity's name=
        # tag (case-insensitive), boost the score.  This helps exact-name
        # and partial-name matches that the embedding might not surface.
        name_boost_score = 0.0
        if _query_name_terms and entity_name:
            name_lower = entity_name.lower()
            for term in _query_name_terms:
                if term in name_lower:
                    name_boost_score += _NAME_SUBSTRING_BOOST

        # Apply semantic distance threshold, but allow cross-script or
        # name-substring matches to bypass it — FastText cc.en.300 distance
        # is meaningless for Korean text (OOV collapse), and substring
        # matches are exact by definition.
        if name_distance > name_distance_threshold:
            if xscript_score <= 0.0 and name_boost_score <= 0.0:
                continue

        name_score = 1.0 - name_distance

        # USLP geographic space score (Mann et al. 2023 §3.3):
        # Geohash cluster centers at P4 (~39km cells), d_max = P4 cell width.
        # The semantic search view has no template context, so P4 (local) is
        # the default precision — appropriate for most tag-based queries.
        geo_score = 0.0
        geo_value = getattr(entity, "geo_distance", None)
        if geo_value is not None and point is not None:
            try:
                if hasattr(geo_value, "m"):
                    dist_m = float(geo_value.m)
                else:
                    dist_m = float(geo_value)
                # Use geohash cluster centers, not raw coordinates
                import geohash2
                gh_h = geohash2.encode(point.y, point.x, precision=4)
                gh_t = geohash2.encode(entity.geom.y, entity.geom.x, precision=4)
                lat_h, lon_h = geohash2.decode(gh_h)
                lat_t, lon_t = geohash2.decode(gh_t)
                cluster_dist_km = math.sqrt(
                    (float(lat_h) - float(lat_t))**2 +
                    (float(lon_h) - float(lon_t))**2
                ) * 111.0  # rough km conversion
                d_max = 39.0  # P4 cell width
                geo_score = max(0.0, min(1.0, 1.0 - (cluster_dist_km / d_max)))
            except (TypeError, ValueError, Exception):
                geo_score = 0.0

        class_score = 0.0
        if rdf_type:
            if entity.wkg_class == rdf_type or (
                entity.wkg_superclasses and rdf_type in entity.wkg_superclasses
            ):
                class_score = 1.0

        # Tag match boost — when query_tags has specific values, boost
        # entities whose tag value exactly matches. This ensures that
        # {"cuisine": "jamaican"} ranks cuisine=jamaican above cuisine=indian
        # even when their FastText embeddings are semantically similar.
        tag_match_score = 0.0
        if query_tags:
            for qk, qv in query_tags.items():
                if qv and entity.tags.get(qk) == qv:
                    tag_match_score += 1.0

        if use_learned_weights:
            final_score = (
                (w_name * name_score)
                + (w_geo * geo_score)
                + (w_class * class_score)
                + tag_match_score
                + xscript_score
                + name_boost_score
            )
        else:
            final_score = (
                name_score + geo_score + class_score + tag_match_score
                + xscript_score + name_boost_score
            )

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
                    "tag_match_score": _safe_float(tag_match_score),
                    "xscript_score": _safe_float(xscript_score),
                    "name_boost_score": _safe_float(name_boost_score),
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

    Converts a free-text query into a structured plan using the MapQA
    parser (TF-IDF + MultinomialNB). The parsed template, concepts, roles,
    and DAG are returned alongside the legacy bbox/rdf_type fields for
    backward compatibility with worldkg_semantic_triplet_search.
    """
    from semantic_search.services.query_parser_service import QueryParserService

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

    # ── MapQA parser (replaces class_keyword_map heuristic) ──
    parser = QueryParserService.get_instance()
    parsed = parser.parse(query_text)

    # Extract OBJECT concept as primary amenity for backward-compat rdf_type
    primary_rdf_type = None
    rdf_types = []
    object_concept = None
    for c in parsed.get("concepts", []):
        if c["type"] == "OBJECT" and c.get("text"):
            object_concept = c["text"]
            break

    # Map common amenity strings to wkgs: classes for backward compat
    amenity_to_class = {
        "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
        "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
        "hotel": "wkgs:Hotel", "resort": "wkgs:Hotel",
        "hospital": "wkgs:Hospital", "clinic": "wkgs:Hospital",
        "school": "wkgs:School", "university": "wkgs:School", "college": "wkgs:School",
        "shop": "wkgs:Shop", "store": "wkgs:Shop", "mall": "wkgs:Shop", "market": "wkgs:Shop",
        "bar": "wkgs:Amenity", "pub": "wkgs:Amenity", "amenity": "wkgs:Amenity",
    }
    if object_concept:
        key = object_concept.lower().replace(" ", "_")
        primary_rdf_type = amenity_to_class.get(key, "wkgs:Amenity")
        rdf_types = [primary_rdf_type]

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
        # ── MapQA parser fields (new) ──
        "template": parsed.get("template"),
        "concepts": parsed.get("concepts"),
        "roles": parsed.get("roles"),
        "dag": parsed.get("dag"),
        "confidence": parsed.get("confidence"),
        "validation": parsed.get("validation"),
    }

    return Response(plan)


@api_view(["POST"])
def execute_query(request):
    """Execute a natural-language geospatial query end-to-end.

    POST /api/nca/execute-query/
    Body: {
        "query": "Which bars are within 50m of Hollywood Blvd?",
        "country_code": "US",          // optional
        "snapshot_date": "2025_12_31", // optional
    }

    Pipeline: parse (QueryParserService) → execute (QueryExecutorService) → answer

    Returns: {query, country_code, parsed, result}
    """
    from semantic_search.services.query_parser_service import QueryParserService
    from semantic_search.services.query_executor_service import QueryExecutorService

    query_text = (request.data.get("query") or "").strip()
    country_code = request.data.get("country_code")
    snapshot_date = request.data.get("snapshot_date")

    if not query_text:
        return Response({"error": "query required"}, status=status.HTTP_400_BAD_REQUEST)

    # Normalize country_code to ISO if provided
    if country_code:
        try:
            country_code = resolve_iso_code(country_code)
        except Exception:
            pass  # use as-is if resolution fails

    # Phase 1: parse
    parser = QueryParserService.get_instance()
    parsed = parser.parse(query_text)

    # Phase 2: execute
    result = QueryExecutorService.execute(
        parsed, country_code, snapshot_date, question=query_text
    )

    return Response({
        "query": query_text,
        "country_code": country_code,
        "parsed": parsed,
        "result": result,
    })


@api_view(["GET"])
def factor_availability(request):
    """Factor-table coverage for a (country, snapshot) — the G4 check.

    GET /api/nca/factor-availability/?country_code=BZ&snapshot_date=2025_12_31

    Returns booleans per latent-space table so an agent can verify data
    availability BEFORE proposing a branch that would fail G4:
        {country_code, snapshot_date,
         spectral, drift, amenity_embeddings, entity_embeddings}
    """
    country_code = (request.GET.get("country_code") or "").strip()
    snapshot_date = request.GET.get("snapshot_date")
    if not country_code:
        return Response({"error": "country_code required"},
                        status=status.HTTP_400_BAD_REQUEST)
    # Normalize to ISO first (override-aware — handles region names like
    # "Ireland And Northern Ireland" → IE) so the G4 booleans match the
    # factor rows' ISO country_code.
    try:
        resolved = resolve_iso_code(country_code)
        if resolved:
            country_code = resolved
    except Exception:
        pass
    country_code = country_code.upper()
    if not snapshot_date:
        snapshot_date = get_latest_snapshot_id()

    from django.db import connections
    cur = connections["vectors"].cursor()

    def exists(table, where, params):
        cur.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params)
        return cur.fetchone() is not None

    spectral = exists(
        "factor_spectral_node_metric",
        "country_code = %s AND snapshot_id = %s",
        [country_code, snapshot_date],
    )
    # DriftNodeMetric keys on snapshot_to_id (no snapshot_id column).
    drift = exists(
        "factor_drift_node_metric",
        "country_code = %s AND snapshot_to_id = %s",
        [country_code, snapshot_date],
    )
    amenity_embeddings = exists(
        "factor_amenity_embedding", "1 = 1", [],
    )
    entity_embeddings = exists(
        "semantic_search_osmentity",
        "country_code = %s AND snapshot_id = %s "
        "AND gv_tags_embedding IS NOT NULL",
        [country_code, snapshot_date],
    )
    return Response({
        "country_code": country_code,
        "snapshot_date": snapshot_date,
        "spectral": spectral,
        "drift": drift,
        "amenity_embeddings": amenity_embeddings,
        "entity_embeddings": entity_embeddings,
    })


@require_GET
def execute_query_stream(request):
    """Streaming execute-query over SSE (Server-Sent Events).

    GET /api/nca/execute-query/stream/?query=...&country_code=...&snapshot_date=...

    Emits progressive events so the frontend shows the agent working instead
    of a spinner:
        event: parsed        data: {parsed}
        event: executed      data: {template, result_count, trace}
        event: research      data: {tool, args}
        event: research_out  data: {tool, output}
        event: answer_delta  data: {delta}
        event: done          data: {result}          (full result JSON)
        event: error         data: {error}

    The pipeline runs in a worker thread; events flow through a queue to the
    response generator (StreamingHttpResponse). Same phases as
    ``execute_query`` — parser → executor → LLM enrichment research loop.
    """
    query_text = (request.GET.get("query") or "").strip()
    country_code = request.GET.get("country_code") or None
    snapshot_date = request.GET.get("snapshot_date") or None

    if not query_text:
        return JsonResponse({"error": "query required"}, status=400)

    def event_stream():
        events = queue.Queue(maxsize=128)

        def emit(event, **payload):
            # Accept both call forms: emit("name", key=val) from this view
            # and event_callback({"event": "name", ...}) from the executor.
            # The executor's dict form is the enrichment/executor contract —
            # without normalization the whole dict lands in the SSE event
            # name and EventSource named listeners never fire.
            if isinstance(event, dict):
                payload = {k: v for k, v in event.items() if k != "event"}
                event = event.get("event") or "message"
            events.put({"event": event, **payload})

        def run():
            try:
                from semantic_search.services.query_parser_service import QueryParserService
                from semantic_search.services.query_executor_service import QueryExecutorService

                # Local aliases — assigning to the closure vars would make
                # them local to run() (UnboundLocalError on read).
                cc = country_code
                if cc:
                    try:
                        cc = resolve_iso_code(cc)
                    except Exception:
                        pass  # use as-is if resolution fails

                parser = QueryParserService.get_instance()
                parsed = parser.parse(query_text)
                emit("parsed", parsed=parsed)

                result = QueryExecutorService.execute(
                    parsed, cc, snapshot_date,
                    question=query_text, event_callback=emit,
                )
                emit("done", result=result)
            except Exception as exc:  # noqa: BLE001 — surface errors as an SSE event
                emit("error", error=str(exc))
            finally:
                events.put(None)  # sentinel

        threading.Thread(target=run, daemon=True).start()

        while True:
            item = events.get()
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item, default=str)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


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
    from core.models import SubgraphProfile, CountryPipelineProfile

    country_code = request.query_params.get('country_code')
    if not country_code:
        return Response(
            {"error": "country_code query parameter required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Normalize to ISO first (override-aware — handles Geofabrik region
    # names like "Ireland And Northern Ireland" → IE), consistent with
    # execute_query / execute_query_stream.
    try:
        resolved = resolve_iso_code(country_code)
        if resolved:
            country_code = resolved
    except Exception:
        pass  # fall through to the profile lookups below

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
