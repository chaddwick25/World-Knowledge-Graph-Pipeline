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
    # Commercial / services / transit / infrastructure targets (2026-10-01)
    # — the place-report recipe's slots (shop, services, getting around,
    # infrastructure) filter by the real OSM key=value, not the fuzzy
    # embedding tier.
    "fuel": ("amenity", "fuel"),
    "fuel station": ("amenity", "fuel"),
    "gas station": ("amenity", "fuel"),
    "charging station": ("amenity", "charging_station"),
    "atm": ("amenity", "atm"),
    "post office": ("amenity", "post_office"),
    "supermarket": ("shop", "supermarket"),
    "marketplace": ("amenity", "marketplace"),
    "fast food": ("amenity", "fast_food"),
    "parking": ("amenity", "parking"),
    "bus stop": ("highway", "bus_stop"),
    "bus station": ("amenity", "bus_station"),
    "police": ("amenity", "police"),
    "library": ("amenity", "library"),
    "cinema": ("amenity", "cinema"),
    "kindergarten": ("amenity", "kindergarten"),
    # Commercial generalization (2026-10-01): markets, bakeries, kiosks,
    # food courts, vending, and the taco/mexican cuisine family resolve to
    # real OSM keys. Cuisine words map to the restaurant amenity (coarse —
    # the data rarely separates taco spots by tag); the research matcher's
    # TAG_RULES cuisine families carry the subtype nuance for evidence and
    # the assembler states when the class does not distinguish.
    "market": ("amenity", "marketplace"),
    "bakery": ("shop", "bakery"),
    "kiosk": ("shop", "kiosk"),
    "food court": ("amenity", "food_court"),
    "food truck": ("amenity", "fast_food"),
    "vending machine": ("amenity", "vending_machine"),
    "convenience store": ("shop", "convenience"),
    "mall": ("shop", "mall"),
    "taco": ("amenity", "restaurant"),
    "tacos": ("amenity", "restaurant"),
    "taqueria": ("amenity", "restaurant"),
    "mexican": ("amenity", "restaurant"),
    # Generic food intent: "Which food vendors..." → the restaurant class
    # (coarse — the data rarely separates vendors by tag).
    "food": ("amenity", "restaurant"),
    # Food-vendor family expansion (2026-10-02) — the food report recipe's
    # vendor vocabulary: drink spots, street vendors, snacks, chains.
    # Coarse mappings follow the taco → restaurant convention: OSM has no
    # "tavern"/"cookout" amenity value, so they resolve to the nearest
    # vendor tag and the cuisine family carries the subtype.
    "tavern": ("amenity", "pub"),
    "beer garden": ("amenity", "biergarten"),
    "biergarten": ("amenity", "biergarten"),
    "nightclub": ("amenity", "nightclub"),
    "club": ("amenity", "nightclub"),
    "ice cream": ("amenity", "ice_cream"),
    "snack bar": ("amenity", "fast_food"),
    "street food": ("amenity", "fast_food"),
    "food stand": ("amenity", "fast_food"),
    "food stall": ("amenity", "marketplace"),
    "bbq": ("amenity", "restaurant"),
    "cookout": ("amenity", "restaurant"),
}

# Eat/drink vendor amenity values — the food report's entity-census
# allowlist (shops, health, transit, and infrastructure are out of scope).
# The "food vendor" TAG_RULES families reuse it below.
FOOD_VENDOR_AMENITIES = (
    "restaurant", "cafe", "fast_food", "bar", "pub", "tavern",
    "biergarten", "food_court", "ice_cream", "nightclub",
    "vending_machine", "marketplace",
)

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
    # Commercial / services / transit / infrastructure intents (2026-10-01)
    # — the place-report recipe's research matcher (slot intents like
    # "getting around" / "infrastructure" re-ask via these families).
    "bus stop": (("highway", ("bus_stop",)),
                 ("amenity", ("bus_station",))),
    "bus station": (("amenity", ("bus_station",)),
                    ("highway", ("bus_stop",))),
    "fuel": (("amenity", ("fuel",)),),
    "charging station": (("amenity", ("charging_station",)),),
    "atm": (("amenity", ("atm",)),),
    "post office": (("amenity", ("post_office", "post_box")),),
    "supermarket": (("shop", ("supermarket",)),
                    ("amenity", ("marketplace",))),
    "marketplace": (("amenity", ("marketplace",)),),
    "parking": (("amenity", ("parking",)),),
    "fast food": (("amenity", ("fast_food",)),),
    "library": (("amenity", ("library",)),),
    "cinema": (("amenity", ("cinema",)),),
    "police": (("amenity", ("police",)),),
    "kindergarten": (("amenity", ("kindergarten",)),),
    # Commercial + cuisine families (2026-10-01) — mirrors the jerk
    # pattern: the cuisine tag (multi-value "tacos;tortas") carries the
    # subtype a coarse wkgs:Amenity cannot.
    "taco": (("amenity", ("restaurant", "fast_food")),
             ("cuisine", ("taco", "tacos", "mexican", "taqueria", "tortas",
                          "antojitos", "tortilla"))),
    "tacos": (("amenity", ("restaurant", "fast_food")),
              ("cuisine", ("taco", "tacos", "mexican", "taqueria", "tortas",
                           "antojitos", "tortilla"))),
    "mexican": (("cuisine", ("taco", "tacos", "mexican", "taqueria", "tortas",
                             "antojitos", "tortilla")),
                ("amenity", ("restaurant", "fast_food"))),
    "market": (("amenity", ("marketplace",)),
               ("shop", ("convenience", "mall", "kiosk"))),
    "bakery": (("shop", ("bakery",)), ("amenity", ("bakery",))),
    "kiosk": (("shop", ("kiosk",)),),
    "food court": (("amenity", ("food_court", "fast_food")),),
    "vending machine": (("amenity", ("vending_machine",)),),
    "convenience store": (("shop", ("convenience",)),
                          ("amenity", ("convenience_store",))),
    "food": (("amenity", ("restaurant", "fast_food", "food_court")),),
    # Food-vendor families (2026-10-02) — drink spots, street vendors,
    # snacks, chains. Generic "food vendor"/"eatery" spans the whole
    # amenity allowlist; cuisine words (bbq/cookout) match the cuisine
    # tag for the subtype the amenity tag can't carry.
    "pub": (("amenity", ("pub", "bar")),),
    "tavern": (("amenity", ("pub", "bar")),),
    "beer garden": (("amenity", ("biergarten", "bar")),),
    "biergarten": (("amenity", ("biergarten", "bar")),),
    "nightclub": (("amenity", ("nightclub", "bar")),),
    "club": (("amenity", ("nightclub", "bar")),),
    "ice cream": (("amenity", ("ice_cream",)),),
    "snack bar": (("amenity", ("fast_food", "food_court")),),
    "street food": (("amenity", ("fast_food", "food_court",
                                 "marketplace")),),
    "food stand": (("amenity", ("fast_food", "food_court")),),
    "food stall": (("amenity", ("marketplace", "fast_food")),),
    "food truck": (("amenity", ("fast_food",)),),
    "bbq": (("amenity", ("restaurant", "fast_food")),
            ("cuisine", ("bbq", "barbecue", "grill"))),
    "cookout": (("amenity", ("restaurant", "fast_food")),
                ("cuisine", ("bbq", "barbecue", "grill"))),
    "food vendor": (("amenity", FOOD_VENDOR_AMENITIES),),
    "eatery": (("amenity", FOOD_VENDOR_AMENITIES),),
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


