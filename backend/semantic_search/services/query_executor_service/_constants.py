"""QueryExecutor constants.

Extracted from query_executor_service.py (monolith split, Phase 5).
"""

import re

***REMOVED*** ── Amenity resolution maps ──────────────────────────────────────────────
***REMOVED*** Free-text phrase → canonical OSM amenity tag value. Checked BEFORE any
***REMOVED*** DB query so common categories never trigger a partition scan (an
***REMOVED*** unscoped `tags__amenity` exists() scans every snapshot partition —
***REMOVED*** ~52s on LK after multiple countries were processed).
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

***REMOVED*** Known amenity keys (values are wkgs classes — used as a set of valid
***REMOVED*** tag values; the resolved tag is the singularized key itself).
AMENITY_TO_WKGS = {
    "cafe": "wkgs:Cafe", "coffee_shop": "wkgs:Cafe",
    "restaurant": "wkgs:Restaurant", "diner": "wkgs:Restaurant",
    "hotel": "wkgs:Hotel", "resort": "wkgs:Hotel",
    "hospital": "wkgs:Hospital", "clinic": "wkgs:Hospital",
    "school": "wkgs:School", "university": "wkgs:School",
    "shop": "wkgs:Shop", "store": "wkgs:Shop", "mall": "wkgs:Shop",
    "bar": "wkgs:Amenity", "pub": "wkgs:Amenity",
}

***REMOVED*** ── Category → real OSM tag target ───────────────────────────────────────
***REMOVED*** Natural category token → the OSM key=value the entity actually carries.
***REMOVED*** The executor's class filter is amenity-key-only today; these targets let
***REMOVED*** "Which hotels/peaks/waterfalls..." filter by the correct key
***REMOVED*** (tourism=hotel, natural=peak, waterway=waterfall) instead of falling to
***REMOVED*** the fuzzy embedding tier. Keys are space-normalized ("guest house",
***REMOVED*** "nature reserve") so the resolver's singularize+underscore normalization
***REMOVED*** round-trips.
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
    ***REMOVED*** Commercial / services / transit / infrastructure targets (2026-10-01)
    ***REMOVED*** — the place-report recipe's slots (shop, services, getting around,
    ***REMOVED*** infrastructure) filter by the real OSM key=value, not the fuzzy
    ***REMOVED*** embedding tier.
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
    ***REMOVED*** Commercial generalization (2026-10-01): markets, bakeries, kiosks,
    ***REMOVED*** food courts, vending, and the taco/mexican cuisine family resolve to
    ***REMOVED*** real OSM keys. Cuisine words map to the restaurant amenity (coarse —
    ***REMOVED*** the data rarely separates taco spots by tag); the research matcher's
    ***REMOVED*** TAG_RULES cuisine families carry the subtype nuance for evidence and
    ***REMOVED*** the assembler states when the class does not distinguish.
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
    ***REMOVED*** Bubble tea (2026-10-11): cuisine-subtype target — the entities are
    ***REMOVED*** amenity=cafe/shop carrying cuisine=bubble_tea; the cuisine key is
    ***REMOVED*** the precise filter (the jerk/taco convention: the subtype rides the
    ***REMOVED*** cuisine tag, not the amenity).
    "bubble tea": ("cuisine", "bubble_tea"),
    ***REMOVED*** Generic food intent: "Which food vendors..." → the restaurant class
    ***REMOVED*** (coarse — the data rarely separates vendors by tag).
    "food": ("amenity", "restaurant"),
    ***REMOVED*** Food-vendor family expansion (2026-10-02) — the food report recipe's
    ***REMOVED*** vendor vocabulary: drink spots, street vendors, snacks, chains.
    ***REMOVED*** Coarse mappings follow the taco → restaurant convention: OSM has no
    ***REMOVED*** "tavern"/"cookout" amenity value, so they resolve to the nearest
    ***REMOVED*** vendor tag and the cuisine family carries the subtype.
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

***REMOVED*** Eat/drink vendor amenity values — the food report's entity-census
***REMOVED*** allowlist (shops, health, transit, and infrastructure are out of scope).
***REMOVED*** The "food vendor" TAG_RULES families reuse it below.
FOOD_VENDOR_AMENITIES = (
    "restaurant", "cafe", "fast_food", "bar", "pub", "tavern",
    "biergarten", "food_court", "ice_cream", "nightclub",
    "vending_machine", "marketplace",
)

***REMOVED*** Intent → candidate OSM tag families, in priority order. The research
***REMOVED*** matcher checks an entity's tags against these BEFORE the coarse
***REMOVED*** wkg_class (Grand Lido Negril is wkgs:Building with tourism=hotel).
***REMOVED*** Multi-value tags are ";"-joined in OSM (cuisine="regional;chicken") —
***REMOVED*** substring match on the value covers that form.
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
    ***REMOVED*** Cuisine intents match the cuisine tag (multi-value "regional;chicken")
    ***REMOVED*** in addition to the restaurant amenity — a grocery store (Hi-Lo Food
    ***REMOVED*** Store, 2026-09-30) must not pass as a "jerk restaurant".
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
    ***REMOVED*** Commercial / services / transit / infrastructure intents (2026-10-01)
    ***REMOVED*** — the place-report recipe's research matcher (slot intents like
    ***REMOVED*** "getting around" / "infrastructure" re-ask via these families).
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
    ***REMOVED*** Commercial + cuisine families (2026-10-01) — mirrors the jerk
    ***REMOVED*** pattern: the cuisine tag (multi-value "tacos;tortas") carries the
    ***REMOVED*** subtype a coarse wkgs:Amenity cannot.
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
    ***REMOVED*** Food-vendor families (2026-10-02) — drink spots, street vendors,
    ***REMOVED*** snacks, chains. Generic "food vendor"/"eatery" spans the whole
    ***REMOVED*** amenity allowlist; cuisine words (bbq/cookout) match the cuisine
    ***REMOVED*** tag for the subtype the amenity tag can't carry.
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
    ***REMOVED*** Bubble tea (2026-10-11): the subtype rides the cuisine tag
    ***REMOVED*** ("bubble_tea" stored; "bubble tea" accepted), mirroring the
    ***REMOVED*** jerk/taco families.
    "bubble tea": (("amenity", ("cafe", "restaurant", "fast_food")),
                   ("cuisine", ("bubble_tea", "bubble tea"))),
    "food vendor": (("amenity", FOOD_VENDOR_AMENITIES),),
    "eatery": (("amenity", FOOD_VENDOR_AMENITIES),),
}

***REMOVED*** Generic plural phrases → "any entity asserting an amenity-type key".
***REMOVED*** Resolved via the GIN-indexed `tags ?|` operator — never an embedding scan.
GENERIC_AMENITY_PHRASES = {
    "amenities", "amenity", "places", "services", "facilities",
    "shop", "shops", "store", "stores", "businesses",
}
GENERIC_AMENITY_KEYS = (
    "amenity", "shop", "tourism", "leisure", "office", "craft",
    "healthcare", "public_transport",
)

***REMOVED*** ── Tag-phrase gate: space ↔ underscore round-trip (2026-10-11) ──────────
***REMOVED*** Natural phrases ("bubble tea") and stored OSM tag values ("bubble_tea")
***REMOVED*** differ by whitespace, and searchers type either form. The gate
***REMOVED*** normalizes both directions: phrase → space form for the space-keyed
***REMOVED*** dicts, value → underscore form for tags__contains filters, and both
***REMOVED*** variants for substring matching. OSM convention stores multi-word tag
***REMOVED*** values underscored; the space form stays as a variant for the rare
***REMOVED*** space-stored value.

_TRAILING_FRAGMENTS = (
    " are", " is", " near", " around", " close", " nearby",
    " within", " in", " of", " at",
    ***REMOVED*** 2026-10-01: "taco vendors" / "food vendors" — the planner rephrases
    ***REMOVED*** vague intents, but the executor stays resilient when one slips
    ***REMOVED*** through.
    " vendors of", " vendors",
)


def strip_trailing_fragments(phrase: str) -> str:
    """Drop the parser's trailing verb/copula fragments ("peak are" →
    "peak"). Shared by _resolve_tag_target and _generic_amenity_keys."""
    phrase = (phrase or "").lower().strip()
    for frag in _TRAILING_FRAGMENTS:
        if phrase.endswith(frag):
            return phrase[:-len(frag)].rstrip()
    return phrase


def normalize_tag_phrase(text: str) -> str:
    """Free-text phrase → space-form canonical phrase (dict-lookup form).

    "Bubble_Tea  are" → "bubble tea". Underscores become spaces so the
    resolver's space-keyed dictionaries round-trip.
    """
    return " ".join(
        strip_trailing_fragments(text).replace("_", " ").split()
    )


def normalize_tag_value(value: str) -> str:
    """Free-text value → the stored OSM convention (underscored).

    "bubble tea" → "bubble_tea". The `name` key is exempt upstream:
    entity names are free text, not tag vocabulary.
    """
    return " ".join((value or "").split()).replace(" ", "_").lower()


def tag_value_variants(text: str) -> tuple:
    """Both forms of a tag value for substring matching, regardless of
    the input's form: "bubble tea" and "bubble_tea" both yield
    ("bubble tea", "bubble_tea")."""
    phrase = " ".join((text or "").split())
    if not phrase:
        return ()
    underscored = phrase.replace(" ", "_").lower()
    return (underscored.replace("_", " "), underscored)

***REMOVED*** ── Default spatial bounds for open-ended questions (no explicit AMOUNT) ──
***REMOVED*** "What X are near/around Y?" → walkable 2km (bounds the candidate pool
***REMOVED*** AND makes the answer spatially honest — the old no-radius path returned
***REMOVED*** country-wide pools). Direction (***REMOVED***5) cone default tightened 20km → 10km.
DEFAULT_NEAR_RADIUS_M = 2000
DIRECTION_NEAREST_RADIUS_M = 10000
PROXIMITY_QUESTION_RE = re.compile(
    r"\b(near|around|close to|nearby|beside|next to|outside)\b", re.I,
)

***REMOVED*** ── USLP geographic scoring (Mann et al. 2023 §3.3) ───────────────────────
***REMOVED*** The paper's geo_score uses geohash cluster centers at relation-specific
***REMOVED*** precision levels, with d_max = per-tail-cluster max distance to any other
***REMOVED*** cluster center at that precision (the per-column max of the distance matrix).
***REMOVED***
***REMOVED*** Geohash precision cell widths (geohash2 reference):
***REMOVED***   P1: ~5000 km  — country/continent level (isInCountry, capitalCity)
***REMOVED***   P3: ~156 km   — state/county/district level (isInCounty, addrState)
***REMOVED***   P4: ~39 km    — local level (addrSuburb, addrHamlet, addrCity)
***REMOVED***
***REMOVED*** For MapQA templates, we use P4 (local) as the default precision since most
***REMOVED*** queries are local-scale. FILTER-AGGREGATE-MEASURE uses the user's explicit
***REMOVED*** radius as d_max with raw haversine (no geohash). OBJECT-FIELD-MEASURE has
***REMOVED*** no geo_score (distance IS the answer).
***REMOVED***
***REMOVED*** d_max fallback when no pool is loaded: the geohash cell width at the
***REMOVED*** precision level (P4 ≈ 39 km). The paper computes d_max from the candidate
***REMOVED*** pool's cluster centers; at runtime without a precomputed pool, the cell
***REMOVED*** width is the closest approximation.
***REMOVED*** TODO: Add these to a yaml file
USLP_GEOHASH_PRECISION = 4
USLP_FALLBACK_D_MAX_KM = {
    1: 5000.0,
    3: 156.0,
    4: 39.0,
}

***REMOVED*** Semantic cutoff for the FastText amenity fallback: keep only entities whose
***REMOVED*** gv_tags embedding is within this cosine distance of the amenity phrase.
***REMOVED*** Without it the tier returns the NEAREST entities of whatever pool the phrase
***REMOVED*** retrieved — e.g. for "bus_station" it surfaced shops near the anchor instead
***REMOVED*** of admitting there were no bus stations in range. 0.5 matches the
***REMOVED*** name-search default (worldkg_nca.views.search name_distance_threshold).
_FASTTEXT_AMENITY_DISTANCE_THRESHOLD = 0.5


