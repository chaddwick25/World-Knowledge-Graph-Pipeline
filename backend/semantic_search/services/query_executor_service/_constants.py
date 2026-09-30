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

# ── Category → real OSM tag target ───────────────────────────────────────
# Natural category token → the OSM key=value the entity actually carries.
# The executor's class filter is amenity-key-only today; these targets let
# "Which hotels/peaks/waterfalls..." filter by the correct key
# (tourism=hotel, natural=peak, waterway=waterfall) instead of falling to
# the fuzzy embedding tier. Keys are space-normalized ("guest house",
# "nature reserve") so the resolver's singularize+underscore normalization
# round-trips.
CATEGORY_TAG_TARGETS = {
    "hotel": ("tourism", "hotel"),
    "guest house": ("tourism", "guest_house"),
    "resort": ("tourism", "resort"),
    "hostel": ("tourism", "hostel"),
    "motel": ("tourism", "motel"),
    "apartment": ("tourism", "apartment"),
    "chalet": ("tourism", "chalet"),
    "attraction": ("tourism", "attraction"),
    "viewpoint": ("tourism", "viewpoint"),
    "museum": ("tourism", "museum"),
    "camp site": ("tourism", "camp_site"),
    "peak": ("natural", "peak"),
    "cave": ("natural", "cave_entrance"),
    "cave entrance": ("natural", "cave_entrance"),
    "beach": ("natural", "beach"),
    "reef": ("natural", "reef"),
    "cliff": ("natural", "cliff"),
    "spring": ("natural", "spring"),
    "lagoon": ("natural", "lagoon"),
    "waterfall": ("waterway", "waterfall"),
    "river": ("waterway", "river"),
    "nature reserve": ("leisure", "nature_reserve"),
    "marina": ("leisure", "marina"),
    "park": ("leisure", "park"),
    "dive centre": ("leisure", "dive_centre"),
    "campsite": ("tourism", "camp_site"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "bar": ("amenity", "bar"),
    "pub": ("amenity", "pub"),
    "school": ("amenity", "school"),
    "bank": ("amenity", "bank"),
    "pharmacy": ("amenity", "pharmacy"),
    "church": ("amenity", "place_of_worship"),
    "hospital": ("amenity", "hospital"),
    "clinic": ("amenity", "clinic"),
}

# Intent → candidate OSM tag families, in priority order. The research
# matcher checks an entity's tags against these BEFORE the coarse
# wkg_class (Grand Lido Negril is wkgs:Building with tourism=hotel).
# Multi-value tags are ";"-joined in OSM (cuisine="regional;chicken") —
# substring match on the value covers that form.
TAG_RULES = {
    "hotel": (("tourism", ("hotel", "guest_house", "resort", "hostel",
                           "motel", "apartment", "chalet")),),
    "cafe": (("amenity", ("cafe",)),),
    "restaurant": (("amenity", ("restaurant", "fast_food")),),
    "bar": (("amenity", ("bar", "pub", "nightclub")),),
    "museum": (("tourism", ("museum",)), ("historic", ("museum",))),
    "church": (("amenity", ("place_of_worship",)),
               ("historic", ("church",))),
    "school": (("amenity", ("school", "college", "university",
                            "kindergarten")),),
    "beach": (("natural", ("beach",)), ("leisure", ("beach_resort",))),
    "park": (("leisure", ("park",)),
             ("landuse", ("recreation_ground", "village_green"))),
    "peak": (("natural", ("peak",)),),
    "waterfall": (("waterway", ("waterfall",)),),
    "cave": (("natural", ("cave_entrance",)),),
    "viewpoint": (("tourism", ("viewpoint",)),),
    "attraction": (("tourism", ("attraction",)),),
    "nature reserve": (("leisure", ("nature_reserve",)),),
    "marina": (("leisure", ("marina",)),),
    "reef": (("natural", ("reef",)),),
    "dive": (("leisure", ("dive_centre",)), ("shop", ("dive",)),
             ("sport", ("scuba_diving",))),
    # Cuisine intents match the cuisine tag (multi-value "regional;chicken")
    # in addition to the restaurant amenity — a grocery store (Hi-Lo Food
    # Store, 2026-09-30) must not pass as a "jerk restaurant".
    "jerk": (("amenity", ("restaurant", "fast_food")),
             ("cuisine", ("jerk", "jamaican", "caribbean", "regional",
                          "chicken"))),
    "jamaican": (("cuisine", ("jerk", "jamaican", "caribbean", "regional",
                              "chicken")),),
    "caribbean": (("cuisine", ("jerk", "jamaican", "caribbean", "regional",
                               "chicken")),),
    "hike": (("natural", ("peak",)),
             ("waterway", ("waterfall",)),
             ("tourism", ("viewpoint", "attraction")),
             ("leisure", ("nature_reserve",))),
    "mountain": (("natural", ("peak",)), ("tourism", ("viewpoint",))),
    "trail": (("route", ("hiking", "foot")),
              ("highway", ("path", "footway")),
              ("tourism", ("viewpoint",))),
    "bank": (("amenity", ("bank",)),),
    "pharmacy": (("amenity", ("pharmacy",)),),
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


