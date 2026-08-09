# TODO: remember to move the docstrings to the notebook
"""
Spatial Link Prediction Service (USLP)

Implements the Unsupervised Spatial Link Prediction (USLP) approach from:
    Mann, Dsouza, Yu, Demidova — "Spatial Link Prediction with Spatial and Semantic
    Embeddings", ISWC 2023.

USLP scores candidate tail entities for a given (head, relation) pair by summing
similarity across three independent spaces:

    score(h, r, t) = geo_score(h, t, r)
                   + name_score(h_literal, t)
                   + class_score(r, t)

Where:
  - geo_score  : Haversine distance between geohash centroids (precision by relation)
  - name_score : FastText cosine similarity between object literal and candidate name
  - class_score: FastText cosine similarity between relation name and candidate class

Primary use-case: filling the missing object-property triples in WorldKG.
WorldKG v1.0 contains *only* datatype properties; this service predicts the
corresponding entity links, e.g.:
    wkg:Leicester  wkgs:isInCounty  "Leicestershire"
    →  predicts → wkg:302324104 (Leicestershire entity)

Acceptance gate: pairs with normalized score ≥ ACCEPTANCE_THRESHOLD (0.7) are stored
as SpatialLink records for downstream use.

[CODE AUDIT TRACEABILITY] 
Maps to logic from: https://github.com/NicolasTe/osm2kg (osm2kg pipeline part 2 -> Spatial link relations)
Implementation: Eliminates the rigid triplet script and implements their exact Tri-Space (Geo, Name, Class) 
summation heuristic mathematically mapped through PGvector and dynamic Geohash arrays mapping back to WorldKG features!

Reference: https://github.com/gkmn21/SSLPandUSLP
"""

import logging
import numpy as np
import geohash2
from typing import List, Dict, Optional, Tuple
from haversine import haversine, Unit
from extraction.services.regional_path_service import normalize_country_name

logger = logging.getLogger(__name__)
# TODO Maybe load these params in a yaml file, that way we can use them in the notebook.
ACCEPTANCE_THRESHOLD = 0.7
# Geohash precision per relation type (shorter = accepts larger spatial distance)
# Aligned with USLP paper: SSLPandUSLP-main/USLP/USLP_main.py:120-135
RELATION_GEOHASH_PRECISION: Dict[str, int] = {
    # Precision 1 — country/continent level (~5,000 km)
    'isIn':           1,
    'addrPlace':      1,
    'isInContinent':  1,
    'country':        1,
    'isInCountry':    1,
    'addrCountry':    1,
    'capitalCity':    1,
    # Precision 3 — state/county/district level (~156 km)
    'addrState':      3,
    'addrDistrict':   3,
    'addrProvince':   3,
    'isInCounty':     3,
    'isInState':      3,
    'isInDistrict':   3,
    # Precision 4 — local level (~39 km)
    'addrSubdistrict': 4,
    'addrSuburb':     4,
    'addrHamlet':     4,
    'addrCity':       4,
    'addrNeighbour':  4,
    'addrVillage':    4,
    'addrTown':       4,
}
DEFAULT_GEOHASH_PRECISION = 4
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


# ---------------------------------------------------------------------------
# Geohash helpers (no external dependency)
# ---------------------------------------------------------------------------

# TODO: refactor
def _encode_geohash(lat: float, lon: float, precision: int) -> str:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    result, bits, bit_idx, char_val = [], 0, 0, 0
    is_lon = True
    while len(result) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                char_val = (char_val << 1) | 1
                lon_range[0] = mid
            else:
                char_val <<= 1
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                char_val = (char_val << 1) | 1
                lat_range[0] = mid
            else:
                char_val <<= 1
                lat_range[1] = mid
        is_lon = not is_lon
        bits += 1
        if bits == 5:
            result.append(_GEOHASH_ALPHABET[char_val])
            bits = 0
            char_val = 0
    return "".join(result)

# TODO: refactor
def _geohash_center(gh: str) -> Tuple[float, float]:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    is_lon = True
    for ch in gh:
        char_val = _GEOHASH_ALPHABET.index(ch)
        for bit in [16, 8, 4, 2, 1]:
            if is_lon:
                mid = (lon_range[0] + lon_range[1]) / 2
                if char_val & bit:
                    lon_range[0] = mid
                else:
                    lon_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if char_val & bit:
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid
            is_lon = not is_lon
    return (lat_range[0] + lat_range[1]) / 2, (lon_range[0] + lon_range[1]) / 2

# TODO: refactor
def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# Spatial relations that carry string literals about geographic entities
# ---------------------------------------------------------------------------
# TODO: double check these values
SPATIAL_LITERAL_TAGS = {
    'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
    'is_in:continent', 'is_in:district',
    'addr:country', 'addr:state', 'addr:county', 'addr:city',
    'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
    'addr:district', 'addr:neighbourhood', 'addr:province',
    'addr:subdistrict', 'addr:place',
    'place', 'capital',
}

TAG_TO_RELATION = {
    # Country-level (precision 1)
    'is_in':              'isIn',
    'is_in:country':      'isInCountry',
    'addr:country':       'addrCountry',
    'is_in:continent':    'isInContinent',
    # State/province/county/district level (precision 3)
    'is_in:state':        'isInState',
    'addr:state':         'addrState',
    'is_in:county':       'isInCounty',
    'addr:county':        'isInCounty',
    'addr:province':      'addrProvince',
    'addr:district':      'addrDistrict',
    'is_in:district':     'isInDistrict',
    # Local level (precision 4)
    'addr:city':          'addrCity',
    'addr:suburb':        'addrSuburb',
    'addr:hamlet':        'addrHamlet',
    'addr:village':       'addrVillage',
    'addr:town':          'addrTown',
    'addr:subdistrict':   'addrSubdistrict',
    'addr:neighbourhood': 'addrNeighbour',
    'addr:place':         'addrPlace',
    'place':              'addrCity',
    'capital':            'capitalCity',
}

# Map relation names to natural language terms for FastText embedding.
# Compound OSM keys like "addrCity" are not in FastText's vocabulary,
# so we map them to natural words that capture the semantic intent.
RELATION_TO_NATURAL_TEXT = {
    'isIn':             'located in',
    'isInCountry':      'country',
    'addrCountry':      'country',
    'isInContinent':    'continent',
    'isInState':        'state province',
    'addrState':        'state province',
    'isInCounty':       'county district',
    'addrProvince':     'province',
    'addrDistrict':     'district',
    'isInDistrict':     'district',
    'addrCity':         'city town',
    'addrSuburb':       'suburb neighborhood',
    'addrHamlet':       'hamlet village',
    'addrVillage':      'village',
    'addrTown':         'town',
    'addrSubdistrict':  'subdistrict',
    'addrNeighbour':    'neighborhood',
    'addrPlace':        'place location',
    'capitalCity':      'capital city',
}


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class SpatialLinkPredictionService:
    """
    USLP-based spatial link prediction for WorldKG completion.

    Workflow:
        1. Call load_candidate_pool() to load geographic entities into memory.
        2. Call predict_links_for_entity(entity) for each entity with spatial literals.
        3. Accepted links (score ≥ threshold) are returned and optionally persisted.

    Example:
        service = SpatialLinkPredictionService()
        service.load_candidate_pool_from_db()
        links = service.predict_links_for_entity(head_entity, threshold=0.7)
    """

    def __init__(self, transe_model_path: Optional[str] = None, entity2id: Optional[Dict] = None, rel2id: Optional[Dict] = None):
        self._pool: List[Dict] = []           # candidate tail entities
        self._ft_model = None                 # lazy FastText model
        self._spatial_index = None            # BallTree for spatial queries
        self._embedding_cache: Dict[str, np.ndarray] = {}  # Cache for FastText embeddings
        
        self.transe_service = None
        if transe_model_path and entity2id and rel2id:
            logger.info(f"USLP: Initializing native TransE mathematical layer from {transe_model_path}")
            try:
                from igea.services.transe_service import TransEGraphEmbeddingService
                self.transe_service = TransEGraphEmbeddingService()
                self.transe_service.load_model(transe_model_path, entity2id, rel2id)
            except ImportError:
                logger.error("Failed to import TransE service.")

    # ------------------------------------------------------------------
    # FastText
    # ------------------------------------------------------------------

    def _get_fasttext(self):
        if self._ft_model is None:
            from semantic_search.services.fasttext_service import FastTextEmbeddingService
            self._ft_model = FastTextEmbeddingService
        return self._ft_model

    def _embed_text(self, text: str) -> np.ndarray:
        """Embed a free-form text string as FastText weighted average (with caching)."""
        # Check cache first
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        
        tokens = normalize_country_name(text).split()
        tag_counts = {t: 1 for t in tokens if t}
        if not tag_counts:
            emb = np.zeros(300, dtype=np.float32)
        else:
            emb = self._get_fasttext().calculate_embedding(tag_counts)
        
        # Cache the result
        self._embedding_cache[text] = emb
        return emb

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    def _build_spatial_index(self):
        """Build BallTree spatial index for fast radius queries."""
        if not self._pool:
            return
        
        from sklearn.neighbors import BallTree
        coords = np.radians([[e['lat'], e['lon']] for e in self._pool])
        self._spatial_index = BallTree(coords, metric='haversine')
        logger.info(f"USLP: Built BallTree spatial index for {len(self._pool):,} candidates")
    
    def load_candidate_pool(self, entities: List[Dict]) -> int:
        """
        Load candidate tail entities.

        Args:
            entities: list of dicts with keys:
                osm_id, lat, lon, tags (dict), wkg_class (str or None),
                gv_tags_embedding (list[float] or None)
        """
        self._pool = entities
        self._build_spatial_index()  # Build spatial index after loading
        logger.info(f"SpatialLinkPredictionService: pool={len(self._pool)} candidates")
        return len(self._pool)

    def load_candidate_pool_from_db(self, limit: int = 200_000, polygon_wkt: Optional[str] = None,
                                    country: Optional[str] = None,
                                    snapshot_id: Optional[str] = None) -> int:
        """Load geographic entities from pgvector DB as candidate pool.

        Args:
            limit: Max number of candidates to load
            polygon_wkt: Optional WKT polygon to spatially filter candidates
            country: Optional country name or ISO code to filter by tags
            snapshot_id: Phase 6 VARCHAR partition key (YYYY_MM_DD) for
                         partition pruning on the partitioned table.
        """
        from worldkg_nca.models import OsmEntity
        from django.contrib.gis.geos import GEOSGeometry

        qs = OsmEntity.objects.using('vectors').filter(geom__isnull=False)

        # Phase 6: partition key filter for pruning
        if snapshot_id:
            qs = qs.filter(snapshot_id=snapshot_id)
        
        # If polygon provided, filter candidates to within the polygon
        if polygon_wkt:
            poly_geom = GEOSGeometry(polygon_wkt, srid=4326)
            qs = qs.filter(geom__within=poly_geom)

        # Pre-compute country tokens (ISO/name/QID) and optional bbox using centralized resolver
        country_tokens = None
        bbox_applied = False
        if country and not polygon_wkt:
            from extraction.services import osm_wikidata_resolver

            country_tokens = set()
            country_upper = country.upper()
            iso_for_bbox = None

            try:
                # Prefer DB-backed relations (handles ISO2 + Wikidata QIDs)
                relations = osm_wikidata_resolver.get_country_relations_dict()
                meta = relations.get(country_upper) or relations.get(country)
            except Exception as exc:
                logger.warning(
                    "USLP: get_country_relations_dict failed for %s: %s",
                    country,
                    exc,
                )
                meta = None

            # Fallback to direct lookups if relations lookup missed
            if not meta:
                meta = (
                    osm_wikidata_resolver.get_country_by_iso(country)
                    or osm_wikidata_resolver.get_country_by_name(country)
                )

            if meta:
                # Canonical country name
                name_val = meta.get("name")
                if name_val:
                    country_tokens.add(str(name_val).upper())

                # ISO2 / canonical code when available — use for CountryPipelineProfile lookup
                iso_code = meta.get("iso_code")
                if iso_code:
                    iso_code_str = str(iso_code).upper()
                    country_tokens.add(iso_code_str)
                    iso_for_bbox = iso_code_str

                # Wikidata ID as a last-resort token
                wikidata_id = meta.get("wikidata_id")
                if wikidata_id:
                    wikidata_upper = str(wikidata_id).upper()
                    country_tokens.add(wikidata_upper)

            # Always include the raw input token so callers like 'jamaica' or 'Q766' still work
            country_tokens.add(country_upper)

            # If we resolved an ISO code, resolve bbox via the canonical
            # CountryPipelineProfile store (populate_bbox_for_profile).
            if iso_for_bbox:
                bbox = None
                try:
                    from django.db import models
                    from orchestration.models import CountryPipelineProfile

                    profile = (
                        CountryPipelineProfile.objects.filter(
                            models.Q(iso2__iexact=iso_for_bbox)
                            | models.Q(iso3__iexact=iso_for_bbox)
                        ).first()
                    )
                except Exception as exc:
                    logger.warning(
                        "USLP: Failed to load CountryPipelineProfile for %s: %s",
                        iso_for_bbox,
                        exc,
                    )
                    profile = None

                if profile:
                    # Use the canonical bbox population function which resolves
                    # from the best available source and persists the result.
                    bbox = osm_wikidata_resolver.populate_bbox_for_profile(profile)

                if bbox:
                    try:
                        wkt = osm_wikidata_resolver.bbox_to_wkt(*bbox)
                        poly_geom = GEOSGeometry(wkt, srid=4326)
                        qs = qs.filter(geom__within=poly_geom)
                        bbox_applied = True
                        logger.info(
                            "USLP: Applied bbox for country=%s (iso=%s, bbox=%s)",
                            country,
                            iso_for_bbox,
                            bbox,
                        )
                    except Exception as exc:
                        logger.warning(
                            "USLP: Failed to apply bbox for %s (%s): %s",
                            country,
                            iso_for_bbox,
                            exc,
                        )
                if not bbox:
                    raise RuntimeError(
                        f"USLP: Cannot resolve bbox for country '{country}' (iso={iso_for_bbox}). "
                        f"No bbox found in CountryPipelineProfile or any fallback source. "
                        f"Run prebuild_worldkg_structure to populate the profile bbox. "
                        f"Processing without spatial filtering would load ALL entities (planet-scale), "
                        f"which is not acceptable for country-level USLP."
                    )
        
        qs = qs[:limit]
        entities = []
        for e in qs.iterator(chunk_size=1_000):
            tags = e.tags or {}
            
            # Apply country tag filter in Python (JSONField doesn't support efficient key-value filtering).
            # If a bbox filter has already been applied, skip tag-based filtering to avoid over-restricting.
            if country and not polygon_wkt and country_tokens and not bbox_applied:
                tag_country = (tags.get('addr:country') or '').upper()
                is_in_country = (tags.get('is_in:country') or '').upper()
                is_in = (tags.get('is_in') or '').upper()
                if not any(
                    token == tag_country
                    or token == is_in_country
                    or (token and token in is_in)
                    for token in country_tokens
                ):
                    continue
            
            entities.append({
                'osm_id': e.osm_id,
                'lat': e.geom.y,
                'lon': e.geom.x,
                'tags': tags,
                'wkg_class': e.wkg_class,
                'gv_tags_embedding': list(e.gv_tags_embedding) if e.gv_tags_embedding is not None else None,
            })
        return self.load_candidate_pool(entities)

    # ------------------------------------------------------------------
    # Three-space scoring (USLP Section 3.3)
    # ------------------------------------------------------------------

    def _geo_score(self, head_lat: float, head_lon: float,
                   tail_lat: float, tail_lon: float,
                   relation: str) -> float:
        """
        Geographic space score using geohash centroids and Haversine distance.

        Score = 1 / (1 + dist_km(geohash_center(h), geohash_center(t)))
        where precision is selected per relation type.
        
        Uses fast geohash2 library (C-based) instead of pure Python implementation.
        """
        precision = RELATION_GEOHASH_PRECISION.get(relation, DEFAULT_GEOHASH_PRECISION)
        
        # Use geohash2 library for fast encoding (C-based, 100x faster than pure Python)
        gh_h = geohash2.encode(head_lat, head_lon, precision=precision)
        gh_t = geohash2.encode(tail_lat, tail_lon, precision=precision)
        
        # Decode to centers (returns strings, need to convert to floats)
        lat_h_str, lon_h_str = geohash2.decode(gh_h)
        lat_t_str, lon_t_str = geohash2.decode(gh_t)
        c_h = (float(lat_h_str), float(lon_h_str))
        c_t = (float(lat_t_str), float(lon_t_str))
        
        # Calculate haversine distance
        dist_km = haversine(c_h, c_t, unit=Unit.KILOMETERS)
        return 1.0 / (1.0 + dist_km)

    def _name_score(self, literal_string: str, candidate: Dict) -> float:
        """
        Name space score: FastText cosine between literal and candidate name tokens.

        Mirrors USLP name space: cosine(FastText(literal), FastText(rdfs:label + nameEn)).
        """
        emb_literal = self._embed_text(literal_string)
        tags = candidate.get('tags', {})
        name = tags.get('name', tags.get('name:en', ''))
        if not name:
            return 0.0
        emb_name = self._embed_text(name)
        # Paper (space2_score): raw cosine_similarity, no clamping.
        # Negative cosine means antonym relation—preserve that signal.
        return float(_cosine(emb_literal, emb_name))

    def _class_score(self, relation: str, candidate: Dict) -> float:
        """
        Class space score: FastText cosine between relation name and candidate class.

        PRESERVES the paper's class space from:
            papers/SSLPandUSLP-main/USLP/approach_utils.py:space3_score()

        The paper uses pre-trained FastText embeddings for relation URIs and type URIs
        (stored in type_and_relation_embeddings_file.h5). Our implementation mirrors this
        by computing FastText cosine between:
          - relation name mapped to natural text (e.g., "isInCountry" → "country")
          - candidate wkg_class (e.g., "wkgs:Country" → "Country")

        NOTE: This class space uses the USLP paper's relation classes (isInCountry,
        addrCity, addrSuburb) — NOT the WorldKG TTL ontology classes (wkgs:Restaurant,
        wkgs:Highway). The TTL classes serve the semantic axis (300D GV-Tags) at query
        time. The USLP relations serve as spatial boundary operators for the 100D space.
        See .schematics_rules.md for the Two-Axis Architecture.

        Falls back to OSM tags when wkg_class is not populated.
        """
        wkg_class = candidate.get('wkg_class')
        if wkg_class:
            class_text = wkg_class.replace('wkgs:', '').replace(':', ' ')
        else:
            # Fallback: derive class-like text from OSM tags
            class_text = self._derive_class_text_from_tags(candidate.get('tags', {}))
            if not class_text:
                return 0.0
        # Map relation to natural language text for FastText
        rel_text = RELATION_TO_NATURAL_TEXT.get(relation, relation)
        emb_rel = self._embed_text(rel_text)
        emb_cls = self._embed_text(class_text)
        # Paper (space3_score): raw cosine_similarity, no clamping.
        # Negative cosine means relation and type are semantically opposite.
        return float(_cosine(emb_rel, emb_cls))

    @staticmethod
    def _derive_class_text_from_tags(tags: Dict) -> str:
        """Derive a class-like text from OSM tags for embedding when wkg_class is missing."""
        # Priority order: type-indicating keys
        type_keys = ['amenity', 'building', 'highway', 'leisure', 'landuse',
                     'natural', 'waterway', 'railway', 'aeroway', 'place',
                     'tourism', 'historic', 'shop', 'office', 'craft', 'sport']
        for key in type_keys:
            val = tags.get(key)
            if val:
                return val
        return ''

    def _total_score(self, head_lat: float, head_lon: float, head_osm_id: int,
                     relation: str, literal: str,
                     candidate: Dict) -> Tuple[float, float]:
        """Sum of three USLP spaces (equal weight, paper Section 3.3).

        Paper (Section 3.3, Fig 3): final_score = geo + name + class  (no normalization)
        Paper does NOT apply a threshold — USLP is evaluated via ranking metrics (Hits@k, MRR).
        
        Implementation: unnormalized = g + n + c  (same as paper)
                        normalized   = unnormalized / 3.0  (÷3 not in paper, but preserves ranking)
                        ACCEPTANCE_THRESHOLD = 0.7 applied to normalized (= 2.1 unnormalized)

        The /3 normalization does not affect ranking (monotonic), only shifts the threshold scale.
        See papers/SSLPandUSLP-main/USLP/approach_utils.py for the original scoring.

        Returns:
            (unnormalized_score, normalized_score) tuple.
            unnormalized_score = g + n + c  (range [0, 3.0])  ← matches paper
            normalized_score   = unnormalized_score / 3.0  (range [0, 1.0])  ← implementation
        """
        g = self._geo_score(head_lat, head_lon,
                            candidate['lat'], candidate['lon'], relation)
        n = self._name_score(literal, candidate)
        
        c = 0.0
        # Restore OpenKE Mathematical intent!
        if self.transe_service:
            # TransE returns distance (lower = better). We invert to similarity probability.
            dist = self.transe_service.score_triple(
                str(head_osm_id), relation, str(candidate['osm_id'])
            )
            # Normalize to [0,1] space using basic inverse distance mapping
            c = 1.0 / (1.0 + dist)
        else:
            c = self._class_score(relation, candidate)
            
        unnormalized = g + n + c
        normalized = unnormalized / 3.0
        return unnormalized, normalized

    # ------------------------------------------------------------------
    # Entity-level prediction
    # ------------------------------------------------------------------

    def predict_links_for_entity(
        self,
        head_osm_id: int,
        head_lat: float,
        head_lon: float,
        head_tags: Dict,
        threshold: float = ACCEPTANCE_THRESHOLD,
        top_k: int = 5,
    ) -> List[Dict]:
        """
        Predict spatial links for a single OSM entity.

        Inspects the entity's tags for spatial literal properties and scores
        candidate tail entities using USLP tri-space scoring.

        Args:
            head_osm_id:  OSM ID of the head entity
            head_lat:     head entity latitude
            head_lon:     head entity longitude
            head_tags:    OSM tag dict
            threshold:    minimum score to accept a link (default: 0.7)
            top_k:        max links to return per relation

        Returns:
            List of dicts: {head_osm_id, relation, tail_osm_id, score, literal}
        """
        if not self._pool:
            logger.warning("SpatialLinkPredictionService: pool is empty")
            return []

        all_scored = []  # Collect all candidates across all tags

        for tag_key, tag_value in head_tags.items():
            if tag_key not in SPATIAL_LITERAL_TAGS and tag_key not in TAG_TO_RELATION:
                continue
            if not isinstance(tag_value, str) or not tag_value.strip():
                continue

            relation = TAG_TO_RELATION.get(tag_key, tag_key)
            literal = tag_value.strip()

            # Get spatial search radius based on relation type
            # Use geohash precision to determine search radius
            precision = RELATION_GEOHASH_PRECISION.get(relation, DEFAULT_GEOHASH_PRECISION)
            radius_km = {1: 5000, 3: 156, 4: 39}.get(precision, 156)
            
            # Use BallTree to find candidates within radius
            if self._spatial_index is not None:
                query_point = np.radians([[head_lat, head_lon]])
                radius_rad = radius_km / 6371.0  # Earth radius in km
                indices = self._spatial_index.query_radius(query_point, r=radius_rad)[0]
                candidates_to_score = [self._pool[i] for i in indices if self._pool[i]['osm_id'] != head_osm_id]
            else:
                # Fallback to full scan if no index
                candidates_to_score = [c for c in self._pool if c['osm_id'] != head_osm_id]
            
            # Debug: log spatial filtering results for first few entities
            if len(all_scored) == 0:
                logger.info(f"CPU USLP: Entity {head_osm_id} at ({head_lat:.4f}, {head_lon:.4f}), relation={relation}, radius={radius_km}km, candidates_in_radius={len(candidates_to_score)}/{len(self._pool)}")
            
            # Score filtered candidates
            scored_candidates = []
            for candidate in candidates_to_score:
                unnormalized, normalized = self._total_score(head_lat, head_lon, head_osm_id, relation, literal, candidate)
                scored_candidates.append({
                    'candidate': candidate,
                    'unnormalized': unnormalized,
                    'normalized': normalized,
                })
                all_scored.append({
                    'head_osm_id': head_osm_id,
                    'relation': relation,
                    'tail_osm_id': candidate['osm_id'],
                    'unnormalized_score': round(unnormalized, 4),
                    'normalized_score': round(normalized, 4),
                    'score': round(normalized, 4),  # backward-compat alias
                    'literal': literal,
                })
            
            # Debug: log top scores for first few entities (regardless of threshold)
            if len(all_scored) < 5 and scored_candidates:
                scored_candidates.sort(key=lambda x: x['normalized'], reverse=True)
                top3 = scored_candidates[:3]
                logger.info(f"CPU USLP: Entity {head_osm_id} relation={relation} literal={literal} top scores (threshold={threshold}):")
                for i, sc in enumerate(top3):
                    g = self._geo_score(head_lat, head_lon, sc['candidate']['lat'], sc['candidate']['lon'], relation)
                    n = self._name_score(literal, sc['candidate'])
                    c = self._class_score(relation, sc['candidate'])
                    logger.info(f"CPU USLP:   - [{i}] geo={g:.3f}, name={n:.3f}, class={c:.3f}, total={sc['unnormalized']:.3f}, norm={sc['normalized']:.3f}")

        # Sort all candidates by normalized_score and take top_k overall (not per tag)
        all_scored.sort(key=lambda x: x['normalized_score'], reverse=True)
        return all_scored[:top_k]

    def predict_links_batch(
        self,
        entities: List[Dict],
        threshold: float = ACCEPTANCE_THRESHOLD,
        top_k: int = 5,
    ) -> List[Dict]:
        """
        Predict spatial links for a batch of entities.

        Args:
            entities: list of dicts with keys:
                osm_id, lat, lon, tags
            threshold: acceptance threshold
            top_k:     max links per (entity, relation) pair

        Returns:
            Flat list of accepted link dicts.
        """
        all_links = []
        total = len(entities)
        logger.info(f"USLP: Starting batch prediction for {total:,} entities")
        
        for idx, entity in enumerate(entities, 1):
            # Early logging to verify processing starts
            if idx in [1, 10]:
                logger.info(f"USLP: Processing entity #{idx}...")
            
            links = self.predict_links_for_entity(
                head_osm_id=entity['osm_id'],
                head_lat=entity['lat'],
                head_lon=entity['lon'],
                head_tags=entity.get('tags', {}),
                threshold=threshold,
                top_k=top_k,
            )
            all_links.extend(links)
            
            # Progress logging every 100 entities for faster feedback
            if idx % 100 == 0:
                logger.info(f"USLP: Processed {idx:,}/{total:,} entities ({100*idx/total:.1f}%), {len(all_links):,} links found, cache size: {len(self._embedding_cache):,}")
        
        logger.info(f"USLP: Batch complete - {len(all_links):,} total links from {total:,} entities, final cache size: {len(self._embedding_cache):,}")
        return all_links

    def persist_links(self, links: List[Dict], snapshot_id=None, country_name=None, threshold: float = ACCEPTANCE_THRESHOLD, method: str = 'USLP') -> int:
        """
        Persist predicted spatial links as SpatialTripletScore model records.

        Links are split into accepted (>= threshold) and rejected (< threshold) tables.

        Args:
            links:        output of predict_links_batch
            snapshot_id:  UUID of the source TemporalSnapshot
            country_name: Country name for self-describing data
            method:       embedding method identifier (unused, kept for compatibility)

        Returns:
            Number of records created (accepted + rejected).
        """
        from igea.models import SpatialTripletScore, SpatialTripletScoreRejected

        PERSIST_CHUNK = 5000
        accepted_count = 0
        rejected_count = 0
        accepted_buf = []
        rejected_buf = []

        for link in links:
            # Parse OSM ID to extract type and numeric ID
            head_osm_id = str(link['head_osm_id'])
            tail_osm_id = str(link['tail_osm_id'])

            # Extract OSM type (node/way/relation) and numeric ID
            # Format: "node/123456" or just "123456"
            if '/' in head_osm_id:
                head_type, head_id = head_osm_id.split('/', 1)
            else:
                head_type = 'node'
                head_id = head_osm_id

            if '/' in tail_osm_id:
                tail_type, tail_id = tail_osm_id.split('/', 1)
            else:
                tail_type = 'node'
                tail_id = tail_osm_id

            is_accepted = link['normalized_score'] >= threshold

            base_fields = {
                'head_osm_type': head_type,
                'head_osm_id': int(head_id),
                'tail_osm_type': tail_type,
                'tail_osm_id': int(tail_id),
                'relation': link['relation'],
                'geo_score': link.get('geo_score', 0.0),
                'name_score': link.get('name_score', 0.0),
                'topo_score': link.get('topo_score', 0.0),
                'unnormalized_score': link['unnormalized_score'],
                'normalized_score': link['normalized_score'],
                'geohash_precision': RELATION_GEOHASH_PRECISION.get(link['relation'], DEFAULT_GEOHASH_PRECISION),
                'snapshot_id': snapshot_id,
                'country_name': country_name,
            }

            if is_accepted:
                accepted_buf.append(SpatialTripletScore(
                    **base_fields,
                    predicted=True,
                ))
            else:
                rejected_buf.append(SpatialTripletScoreRejected(
                    **base_fields,
                ))

            if len(accepted_buf) >= PERSIST_CHUNK:
                SpatialTripletScore.objects.bulk_create(accepted_buf, ignore_conflicts=True)
                accepted_count += len(accepted_buf)
                accepted_buf.clear()

            if len(rejected_buf) >= PERSIST_CHUNK:
                SpatialTripletScoreRejected.objects.bulk_create(rejected_buf, ignore_conflicts=True)
                rejected_count += len(rejected_buf)
                rejected_buf.clear()

        # Flush remaining
        if accepted_buf:
            SpatialTripletScore.objects.bulk_create(accepted_buf, ignore_conflicts=True)
            accepted_count += len(accepted_buf)

        if rejected_buf:
            SpatialTripletScoreRejected.objects.bulk_create(rejected_buf, ignore_conflicts=True)
            rejected_count += len(rejected_buf)

        logger.info(f"SpatialLinkPredictionService: persisted {accepted_count:,} accepted, {rejected_count:,} rejected ({accepted_count + rejected_count:,} total)")
        return accepted_count + rejected_count
