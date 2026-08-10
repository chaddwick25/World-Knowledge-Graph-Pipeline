"""
Wikidata Candidate Ingestion Service

Harvests geographic entity candidates from the Wikidata SPARQL endpoint
(https://query.wikidata.org/sparql) and converts them into the dict format
expected by IterativeEntityAlignmentService.load_wikidata_candidates().

This fills the gap in the IGEA pipeline:
    load_wikidata_candidates_from_db()  ← recycles already-linked entities (closed loop)
    WikidataCandidateService.harvest_*  ← NEW: fetches fresh candidates externally

Geographic scoping:
    By bounding box:  harvest_by_bbox(min_lon, min_lat, max_lon, max_lat)
    By country code:  harvest_by_country(country_code)

WorldKG class mapping (NCA reverse lookup):
    Wikidata entity types (P31) are reverse-mapped to wkgs: classes using the
    owl:equivalentClass links stored in the Redis ontology cache.
    Example:  wd:Q11707 (restaurant) ← wkgs:Restaurant.wikidata_equivalent
              → candidate['wkg_class'] = 'wkgs:Restaurant'

Reference:
    NCA paper (class alignment):   https://arxiv.org/pdf/2107.13257.pdf  §4.3
    IGEA paper (candidate pool):   https://arxiv.org/pdf/2303.15271.pdf  Algorithm 1
    WorldKG project page:          https://www.vgiscience.org/projects/worldkg.html
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests
from django.db import models

from extraction.services import osm_wikidata_resolver
from orchestration.models import CountryPipelineProfile

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "EDAVectorSearchToolkit/1.0 (WorldKG-IGEA; contact via GitHub)"
BBOX_PAGE_SIZE = 2_000
REQUEST_DELAY_S = 1.1  # Wikidata rate limit: ~1 req/s for anonymous clients
SPARQL_TIMEOUT_S = 120  # wikibase:box queries on large bboxes can take 60-90s
SPARQL_MAX_RETRIES = 1  # retry on 429/502/503 with exponential backoff
SPARQL_BACKOFF_BASE_S = 5  # base delay: 5s, 10s, 20s


def _sparql_request_with_retry(
    query: str,
    use_post: bool = True,
    max_retries: int = SPARQL_MAX_RETRIES,
    backoff_base: float = SPARQL_BACKOFF_BASE_S,
) -> Optional[dict]:
    """Execute a SPARQL request with retry+backoff for transient errors.

    Retries on 429 (Too Many Requests), 502 (Bad Gateway), 503 (Service
    Unavailable), and Timeout. Returns the JSON response dict, or None
    if all retries are exhausted.

    Args:
        query: SPARQL query string.
        use_post: If True, use POST (avoids URL length limits for large
                  VALUES clauses). If False, use GET (for simple queries).
        max_retries: Maximum number of retry attempts.
        backoff_base: Base delay in seconds; retries use backoff_base * 2^attempt.
    """
    for attempt in range(max_retries + 1):
        try:
            if use_post:
                response = requests.post(
                    WIKIDATA_SPARQL_ENDPOINT,
                    data={"query": query, "format": "json"},
                    headers={
                        "User-Agent": WIKIDATA_USER_AGENT,
                        "Accept": "application/sparql-results+json",
                    },
                    timeout=SPARQL_TIMEOUT_S,
                )
            else:
                response = requests.get(
                    WIKIDATA_SPARQL_ENDPOINT,
                    params={"query": query, "format": "json"},
                    headers={"User-Agent": WIKIDATA_USER_AGENT},
                    timeout=SPARQL_TIMEOUT_S,
                )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            # Retry only on transient server errors / rate limiting
            if status_code in (429, 502, 503) and attempt < max_retries:
                delay = backoff_base * (2 ** attempt)
                logger.warning(
                    f"SPARQL request got {status_code}, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                continue
            logger.error(f"SPARQL request failed (HTTP {status_code}): {exc}")
            return None
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                delay = backoff_base * (2 ** attempt)
                logger.warning(
                    f"SPARQL request timed out, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                continue
            logger.error(f"SPARQL request timed out after {max_retries} retries")
            return None
        except requests.exceptions.RequestException as exc:
            logger.error(f"SPARQL request failed: {exc}")
            return None
    return None


class WikidataCandidateService:
    """
    Harvests Wikidata geographic entity candidates for IGEA alignment.

    Candidates are Wikidata entities that carry:
      wdt:P625  (coordinate location) — guarantees geographic grounding
      wdt:P31   (instance of)         — used to map to a wkgs: class via NCA

    Output dict per candidate (matches load_wikidata_candidates() contract):
    {
        'wikidata_uri': 'http://www.wikidata.org/entity/Q12345',
        'lat': 48.137,
        'lon': 11.576,
        'label': 'Marienplatz',
        'wkg_class': 'wkgs:Square',    # None if no NCA reverse mapping found
        'embedding': None,             # caller may populate via FastText later
    }

    Usage:
        svc = WikidataCandidateService(ontology_service=WorldKGOntologyService())
        candidates = svc.harvest_by_country('DE', limit=50_000)
        igea.load_wikidata_candidates(candidates)
    """

    def __init__(
        self,
        ontology_service=None,
        request_delay: float = REQUEST_DELAY_S,
    ):
        """
        Args:
            ontology_service: Optional WorldKGOntologyService for NCA reverse mapping.
                              Without it, all candidates will have wkg_class=None.
            request_delay:    Seconds to sleep between paginated SPARQL requests.
        """
        self.ontology_service = ontology_service
        self.request_delay = request_delay
        self._wikidata_to_wkg: Dict[str, str] = {}  # built lazily on first use

    # ------------------------------------------------------------------
    # Public harvest methods
    # ------------------------------------------------------------------

    def harvest_by_bbox(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        wkg_class_filter: Optional[str] = None,
        limit: int = 50_000,
    ) -> List[Dict]:
        """
        Harvest Wikidata geographic entities within a bounding box.

        Args:
            min_lon, min_lat, max_lon, max_lat: WGS84 bbox corners.
            wkg_class_filter: Restrict to one wkgs: class (e.g. 'wkgs:Restaurant').
                              None = all classes.
            limit: Hard cap on returned candidates.

        Returns:
            List of candidate dicts ready for load_wikidata_candidates().
        """
        self._build_reverse_nca_map()

        candidates: List[Dict] = []
        seen_uris: set = set()  # deduplicate entities that have multiple P31 values
        offset = 0

        while len(candidates) < limit:
            page_size = min(BBOX_PAGE_SIZE, limit - len(candidates))
            batch = self._sparql_bbox_page(
                min_lon, min_lat, max_lon, max_lat,
                offset=offset,
                page_size=page_size,
            )
            if not batch:
                break

            for row in batch:
                cand = self._row_to_candidate(row)
                if cand is None:
                    continue
                uri = cand['wikidata_uri']
                if uri in seen_uris:
                    continue  # already have this entity (duplicate P31 row)
                if wkg_class_filter and cand.get('wkg_class') != wkg_class_filter:
                    seen_uris.add(uri)  # mark as seen even if filtered
                    continue
                seen_uris.add(uri)
                candidates.append(cand)

            if len(batch) < page_size:
                break  # last page reached

            offset += page_size
            time.sleep(self.request_delay)

        logger.info(
            f"WikidataCandidate: harvested {len(candidates):,} candidates "
            f"from bbox [{min_lon:.2f},{min_lat:.2f},{max_lon:.2f},{max_lat:.2f}]"
        )
        return candidates

    def harvest_by_country(
        self,
        country_code: Optional[str],
        wkg_class_filter: Optional[str] = None,
        limit: int = 50_000,
        poly_file_path: Optional[str] = None,
    ) -> List[Dict]:
        """
        Harvest Wikidata candidates for a country.

        Bounding box resolution order:
          1. poly_file_path  → parse .poly file for exact bbox
          2. extraction.OsmBoundary DB record (by iso_code)
          3. extraction.PolygonFile DB record (by country name)

        Args:
            country_code:    ISO 3166-1 alpha-2 (e.g. 'DE', 'GB', 'NG').
            wkg_class_filter: Optional wkgs: class filter.
            limit:           Maximum candidates.
            poly_file_path:  Optional explicit .poly file path.

        Returns:
            List of candidate dicts.
        """
        # Delegate bbox resolution to the shared extraction-level resolver.
        bbox = self._resolve_country_bbox(country_code, poly_file_path)
        if bbox is None:
            logger.error(
                f"WikidataCandidate: cannot resolve bbox for country '{country_code}'. "
                f"Provide --poly-file or add an OsmBoundary/PolygonFile DB record for this country."
            )
            return []

        logger.info(
            f"WikidataCandidate: country={country_code} "
            f"bbox=[{bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}]"
        )
        return self.harvest_by_bbox(
            *bbox,
            wkg_class_filter=wkg_class_filter,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # SPARQL query (paginated)
    # ------------------------------------------------------------------

    def _sparql_bbox_page(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        offset: int = 0,
        page_size: int = BBOX_PAGE_SIZE,
    ) -> List[Dict]:
        """
        Execute one paginated Wikidata SPARQL request using wikibase:box.

        wikibase:box uses Wikidata's built-in geospatial index, making it
        ~100x faster than FILTER(?lat >= ...) which forces a full table scan
        and reliably times out on country-sized bounding boxes.

        Returns raw SPARQL bindings; ?location is a WKT literal
        'Point(lon lat)' parsed by _row_to_candidate.
        """
        # wikibase:box expects Point(lon lat) — note lon FIRST (GeoSPARQL / WKT order).
        # P31 (instance-of) is intentionally EXCLUDED here: adding it forces a JOIN
        # against Wikidata's ~500M-row statement table on top of the spatial result set,
        # causing a 504 Gateway Timeout on country-sized bboxes.
        # P31 enrichment is done as a separate batched phase (enrich_wkg_class).
        query = f"""
SELECT ?entity ?entityLabel ?location WHERE {{
  SERVICE wikibase:box {{
    ?entity wdt:P625 ?location .
    bd:serviceParam wikibase:cornerSouthWest "Point({min_lon} {min_lat})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({max_lon} {max_lat})"^^geo:wktLiteral .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
LIMIT {page_size}
OFFSET {offset}
"""
        try:
            data = _sparql_request_with_retry(query, use_post=False)
            if data is None:
                logger.warning(
                    f"WikidataCandidate: SPARQL request failed at offset={offset} "
                    f"(bbox=[{min_lon},{min_lat},{max_lon},{max_lat}]) after retries."
                )
                return []
            return data.get("results", {}).get("bindings", [])
        except Exception as exc:
            logger.error(f"WikidataCandidate: SPARQL request error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Row → candidate dict conversion
    # ------------------------------------------------------------------

    def _row_to_candidate(self, row: Dict) -> Optional[Dict]:
        """
        Convert a SPARQL result binding row to an IGEA candidate dict.

        Handles the wikibase:box output format where coordinates come as
        a WKT literal in ?location: 'Point(lon lat)' (lon-first per GeoSPARQL).
        """
        entity_uri   = row.get("entity", {}).get("value")
        label        = row.get("entityLabel", {}).get("value", "")
        location_str = row.get("location", {}).get("value")  # 'Point(lon lat)'

        if not entity_uri or not location_str:
            return None

        # Keep only proper Q-entities (skip property/lexeme items)
        if "wikidata.org/entity/Q" not in entity_uri:
            return None

        lat, lon = _parse_wkt_point(location_str)
        if lat is None:
            return None

        return {
            "wikidata_uri": entity_uri,
            "lat": lat,
            "lon": lon,
            "label": label,
            "wkg_class": None,  # populated by enrich_wkg_class() if needed
            "embedding": None,
        }

    # ------------------------------------------------------------------
    # Phase-2: batched P31 → wkg_class enrichment
    # ------------------------------------------------------------------

    def enrich_wkg_class(
        self,
        candidates: List[Dict],
        batch_size: int = 100,
    ) -> List[Dict]:
        """
        Enrich harvested candidates with wkg_class via batched P31 lookups.

        This is a separate phase from harvesting because adding P31 to the
        wikibase:box query causes 504 Gateway Timeout errors on large bboxes.
        Instead, a VALUES-based query is used which lets Wikidata use a hash
        join (O(batch_size)) rather than a full P31 table scan.

        The SPARQL query uses ``wdt:P31/wdt:P279*`` (instance-of then zero-or-more
        subclass-of hops) so that specific Wikidata types like Q5119 (capital city)
        resolve to broader ontology classes like Q515 (city).  A second VALUES
        clause restricts the traversal target to the ~45 known ontology types,
        keeping the query efficient.

        Args:
            candidates: List from harvest_by_bbox / harvest_by_country.
            batch_size: Entities per VALUES query (default 100; Wikidata SPARQL
                rejects GET requests with URLs > ~8KB, so 500 QIDs per batch
                triggers 414/431 errors).

        Returns:
            Same list, with wkg_class populated in-place where a mapping exists.
        """
        self._build_reverse_nca_map()
        if not self._wikidata_to_wkg:
            logger.warning("enrich_wkg_class: NCA map empty, skipping enrichment.")
            return candidates

        # Build the target-type VALUES clause from the NCA reverse map keys.
        # This restricts P279* traversal to only our known ontology types.
        type_values_clause = self._build_type_values_clause()
        if not type_values_clause:
            logger.warning("enrich_wkg_class: no valid type URIs in NCA map.")
            return candidates

        logger.info(
            f"enrich_wkg_class: NCA map has {len(self._wikidata_to_wkg)} type mappings, "
            f"enriching {len(candidates)} candidates in batches of {batch_size}"
        )

        # Index candidates by URI for fast lookup
        uri_to_cands: Dict[str, List[Dict]] = {}
        for c in candidates:
            uri_to_cands.setdefault(c["wikidata_uri"], []).append(c)

        uris = list(uri_to_cands.keys())
        total_enriched = 0
        total_rows_returned = 0
        failed_batches = 0

        for i in range(0, len(uris), batch_size):
            batch_uris = uris[i : i + batch_size]
            # Build VALUES clause using Q-ids only (wd:Q12345)
            values_clause = " ".join(
                f"wd:{u.rstrip('/').rsplit('/', 1)[-1]}"
                for u in batch_uris
                if "/Q" in u
            )
            if not values_clause:
                continue

            # P31/P279* = "instance of, then zero-or-more subclass-of hops".
            # The second VALUES on ?typeUri prunes the traversal to our known
            # ontology types — Wikidata's query planner uses this as a filter.
            query = f"""
SELECT ?entity ?typeUri WHERE {{
  VALUES ?entity {{ {values_clause} }}
  VALUES ?typeUri {{ {type_values_clause} }}
  ?entity wdt:P31/wdt:P279* ?typeUri .
}}
"""
            try:
                data = _sparql_request_with_retry(query, use_post=True)
                if data is None:
                    failed_batches += 1
                    logger.warning(
                        f"enrich_wkg_class batch {i // batch_size}: "
                        f"failed after {SPARQL_MAX_RETRIES} retries"
                    )
                else:
                    rows = data.get("results", {}).get("bindings", [])
                    total_rows_returned += len(rows)
                    for row in rows:
                        entity_uri = row.get("entity", {}).get("value", "")
                        type_uri   = row.get("typeUri", {}).get("value", "")
                        wkg_class  = self._wikidata_to_wkg.get(type_uri)
                        if wkg_class and entity_uri in uri_to_cands:
                            for c in uri_to_cands[entity_uri]:
                                if c["wkg_class"] is None:  # keep first mapping found
                                    c["wkg_class"] = wkg_class
                                    total_enriched += 1
            except Exception as exc:
                failed_batches += 1
                logger.warning(
                    f"enrich_wkg_class batch {i // batch_size}: {exc}"
                )

            time.sleep(self.request_delay)

        total_batches = (len(uris) + batch_size - 1) // batch_size if uris else 0
        logger.info(
            f"enrich_wkg_class: {total_enriched}/{len(candidates)} candidates mapped "
            f"({total_rows_returned} P31/P279* rows returned from Wikidata, "
            f"{failed_batches}/{total_batches} batches failed)"
        )
        return candidates

    def _build_type_values_clause(self) -> str:
        """
        Build a SPARQL VALUES clause fragment containing the Q-IDs of all
        Wikidata types present in the NCA reverse map.

        Returns:
            String like ``wd:Q515 wd:Q3957 wd:Q532 ...`` or empty string.
        """
        qids = []
        for uri in self._wikidata_to_wkg:
            if "/Q" in uri:
                qid = uri.rstrip("/").rsplit("/", 1)[-1]
                # Guard against corrupted Q-IDs (e.g. non-ASCII characters)
                if qid and qid[0] == "Q" and qid[1:].isdigit():
                    qids.append(f"wd:{qid}")
                else:
                    logger.warning(
                        f"enrich_wkg_class: skipping invalid Q-ID '{qid}' "
                        f"from URI '{uri}'"
                    )
        return " ".join(qids)

    # ------------------------------------------------------------------
    # NCA reverse map: Wikidata type URI → wkgs: class
    # ------------------------------------------------------------------

    def _build_reverse_nca_map(self) -> None:
        """
        Build reverse map from Wikidata type URI → wkgs: class name using
        the owl:equivalentClass links stored in the Redis ontology cache.

        WorldKG stores:
          wkgs:Restaurant.wikidata_equivalent = "http://www.wikidata.org/entity/Q11707"
        Reverse map:
          "http://www.wikidata.org/entity/Q11707"  →  "wkgs:Restaurant"

        This is the NCA alignment used in the opposite direction — from Wikidata
        type back to the WorldKG schema namespace.
        """
        if self._wikidata_to_wkg:
            return  # already built, skip

        if self.ontology_service is None:
            logger.debug(
                "WikidataCandidate: no ontology_service supplied; "
                "wkg_class will be None for all candidates."
            )
            return

        try:
            all_classes = self.ontology_service.get_all_classes()
            for cls_name in all_classes:
                wd_uri = self.ontology_service.get_wikidata_equivalent(cls_name)
                if wd_uri:
                    self._wikidata_to_wkg[wd_uri] = cls_name
            logger.info(
                f"WikidataCandidate: NCA reverse map built "
                f"({len(self._wikidata_to_wkg)} Wikidata→wkgs: mappings)"
            )
        except Exception as exc:
            logger.warning(f"WikidataCandidate: could not build NCA map: {exc}")

    # ------------------------------------------------------------------
    # Country bbox resolution
    # ------------------------------------------------------------------

    def _resolve_country_bbox(
        self,
        country_code: Optional[str],
        poly_file_path: Optional[str] = None,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Backward-compatible wrapper around the shared resolver.

        This method is retained temporarily for callers that still
        reference it directly. It simply delegates to
        osm_wikidata_resolver.resolve_country_bbox with the same
        resolution order (poly file → DB geometry → static fallback).
        """

        return osm_wikidata_resolver.resolve_country_bbox(country_code, poly_file_path)


# ------------------------------------------------------------------
# Module-level helpers (importable by train_gv_nle and other consumers)
# ------------------------------------------------------------------

def parse_poly_bbox(poly_path: str) -> Optional[Tuple[float, float, float, float]]:
    """
    Parse an osmium .poly boundary file and return its bounding box.

    .poly format (Geofabrik / OpenStreetMap standard):
        polygon_name
        1                    ← ring number (positive = outer, ! prefix = hole)
            lon1  lat1
            lon2  lat2
            ...
        END
        END

    Returns:
        (min_lon, min_lat, max_lon, max_lat) or None on parse failure.
    """
    try:
        lons, lats = [], []
        with open(poly_path, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2:
                    try:
                        lons.append(float(parts[0]))
                        lats.append(float(parts[1]))
                    except ValueError:
                        continue
        if lons and lats:
            return min(lons), min(lats), max(lons), max(lats)
    except Exception as exc:
        logger.warning(f"parse_poly_bbox({poly_path}): {exc}")
    return None


def parse_poly_to_wkt(poly_path: str) -> Optional[str]:
    """
    Parse an osmium .poly file into a WKT POLYGON string (outer ring only).

    Useful for PostGIS ST_Within geofencing in train_gv_nle and IGEA.

    Returns:
        WKT string like 'POLYGON((lon lat, lon lat, ...))' or None on failure.
    """
    try:
        coords: List[Tuple[float, float]] = []
        reading_outer = False

        with open(poly_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped == "END":
                    if reading_outer and coords:
                        break  # end of first (outer) ring — stop here
                    reading_outer = False
                    continue
                parts = stripped.split()
                if len(parts) == 1:
                    # Ring number line: positive = outer ring, '!' prefix = hole
                    reading_outer = not stripped.startswith("!")
                    continue
                if reading_outer and len(parts) == 2:
                    try:
                        coords.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        pass

        if len(coords) < 3:
            return None

        # Close the ring if open
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        coord_str = ", ".join(f"{lon} {lat}" for lon, lat in coords)
        return f"POLYGON(({coord_str}))"

    except Exception as exc:
        logger.warning(f"parse_poly_to_wkt({poly_path}): {exc}")
        return None


def bbox_to_wkt(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Convert a bounding box to a WKT POLYGON string for PostGIS use."""
    return (
        f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
    )


def _parse_wkt_point(wkt: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse a GeoSPARQL WKT Point literal to (lat, lon).

    Wikidata returns 'Point(lon lat)' — note longitude FIRST.
    Examples:
        'Point(11.576166667 48.137222222)' → lat=48.137, lon=11.576
        '<http://www.wikidata.org/entity/Q1>Point(11.5 48.1)' → lat=48.1, lon=11.5

    Returns (lat, lon) or (None, None) on parse failure.
    """
    import re
    m = re.search(r'Point\(([\d.\-]+)\s+([\d.\-]+)\)', wkt, re.IGNORECASE)
    if not m:
        return None, None
    try:
        lon = float(m.group(1))
        lat = float(m.group(2))
        return lat, lon
    except ValueError:
        return None, None


def _iso_to_name_hint(code: str) -> str:
    """Map ISO 3166-1 alpha-2 code to a PolygonFile region_name fragment."""
    hints = {
        "DE": "germany",       "GB": "great-britain",  "FR": "france",
        "IT": "italy",         "US": "united-states",  "CA": "canada",
        "AU": "australia",     "NL": "netherlands",    "BE": "belgium",
        "AT": "austria",       "CH": "switzerland",    "ES": "spain",
        "PL": "poland",        "SE": "sweden",         "NO": "norway",
        "JP": "japan",         "CN": "china",          "BR": "brazil",
        "ZA": "south-africa",  "NG": "nigeria",        "TZ": "tanzania",
        "JM": "jamaica",       "CR": "costa-rica",     "GT": "guatemala",
        "HN": "honduras",      "SV": "el-salvador",    "NI": "nicaragua",
        "PA": "panama",        "BZ": "belize",         "CU": "cuba",
        "HT": "haiti",         "DO": "dominican-republic", "MX": "mexico",
    }
    return hints.get(code, code.lower())
