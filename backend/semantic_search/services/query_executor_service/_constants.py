"""QueryExecutor constants.

Extracted from query_executor_service.py (monolith split, Phase 5).
"""

import re

# ── Amenity resolution maps ──────────────────────────────────────────────
# Free-text phrase → canonical OSM amenity tag value. Checked BEFORE any
# DB query so common categories never trigger a partition scan (an
# unscoped `tags__amenity` exists() scans every snapshot partition —
# ~52s on LK after multiple countries were processed).
AMENITY_TAG_ALIASES = {
    "police": "police",
    "police_station": "police",
    "fire_station": "fire_station",
    "post_office": "post_office",
    "post_box": "post_box",
    "bank": "bank",
    "atm": "atm",
    "pharmacy": "pharmacy",
    "kindergarten": "kindergarten",
    "college": "college",
    "library": "library",
    "museum": "museum",
    "theatre": "theatre",
    "cinema": "cinema",
    "fuel": "fuel",
    "charging_station": "charging_station",
    "parking": "parking",
    "bus_station": "bus_station",
    "train_station": "train_station",
    "place_of_worship": "place_of_worship",
    "marketplace": "marketplace",
    "doctors": "doctors",
    "dentist": "dentist",
    "veterinary": "veterinary",
    "community_centre": "community_centre",
    "townhall": "townhall",
}

# Known amenity keys (values are wkgs classes — used as a set of valid
# tag values; the resolved tag is the singularized key itself).
AMENITY_TO_WKGS = {
    "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
    "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
    "hotel": "wkgs:Hotel", "resort": "wkgs:Hotel",
    "hospital": "wkgs:Hospital", "clinic": "wkgs:Hospital",
    "school": "wkgs:School", "university": "wkgs:School",
    "shop": "wkgs:Shop", "store": "wkgs:Shop", "mall": "wkgs:Shop",
    "bar": "wkgs:Amenity", "pub": "wkgs:Amenity",
}

# Generic plural phrases → "any entity asserting an amenity-type key".
# Resolved via the GIN-indexed `tags ?|` operator — never an embedding scan.
GENERIC_AMENITY_PHRASES = {
    "amenities", "amenity", "places", "services", "facilities",
    "shop", "shops", "store", "stores", "businesses",
}
GENERIC_AMENITY_KEYS = (
    "amenity", "shop", "tourism", "leisure", "office", "craft",
    "healthcare", "public_transport",
)

# ── Default spatial bounds for open-ended questions (no explicit AMOUNT) ──
# "What X are near/around Y?" → walkable 2km (bounds the candidate pool
# AND makes the answer spatially honest — the old no-radius path returned
# country-wide pools). Direction (#5) cone default tightened 20km → 10km.
DEFAULT_NEAR_RADIUS_M = 2000
DIRECTION_NEAREST_RADIUS_M = 10000
PROXIMITY_QUESTION_RE = re.compile(
    r"\b(near|around|close to|nearby|beside|next to|outside)\b", re.I,
)

# ── USLP geographic scoring (Mann et al. 2023 §3.3) ───────────────────────
# The paper's geo_score uses geohash cluster centers at relation-specific
# precision levels, with d_max = per-tail-cluster max distance to any other
# cluster center at that precision (the per-column max of the distance matrix).
#
# Geohash precision cell widths (geohash2 reference):
#   P1: ~5000 km  — country/continent level (isInCountry, capitalCity)
#   P3: ~156 km   — state/county/district level (isInCounty, addrState)
#   P4: ~39 km    — local level (addrSuburb, addrHamlet, addrCity)
#
# For MapQA templates, we use P4 (local) as the default precision since most
# queries are local-scale. FILTER-AGGREGATE-MEASURE uses the user's explicit
# radius as d_max with raw haversine (no geohash). OBJECT-FIELD-MEASURE has
# no geo_score (distance IS the answer).
#
# d_max fallback when no pool is loaded: the geohash cell width at the
# precision level (P4 ≈ 39 km). The paper computes d_max from the candidate
# pool's cluster centers; at runtime without a precomputed pool, the cell
# width is the closest approximation.
# TODO: Add these to a yaml file
USLP_GEOHASH_PRECISION = 4
USLP_FALLBACK_D_MAX_KM = {
    1: 5000.0,
    3: 156.0,
    4: 39.0,
}

# Semantic cutoff for the FastText amenity fallback: keep only entities whose
# gv_tags embedding is within this cosine distance of the amenity phrase.
# Without it the tier returns the NEAREST entities of whatever pool the phrase
# retrieved — e.g. for "bus_station" it surfaced shops near the anchor instead
# of admitting there were no bus stations in range. 0.5 matches the
# name-search default (worldkg_nca.views.search name_distance_threshold).
_FASTTEXT_AMENITY_DISTANCE_THRESHOLD = 0.5


