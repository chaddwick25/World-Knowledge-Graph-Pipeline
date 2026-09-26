"""MapQA template constants — shared by the generator and the samplers.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).  The samplers live in this
package (one module per template); the constants they reference live here
so both sides resolve them from their own module globals.
"""

# ── Template names ─────────────────────────────────────────────────────────
TEMPLATE_FILTER_AGGREGATE = "FILTER-AGGREGATE-MEASURE (#1)"
TEMPLATE_OBJECT_FIELD = "OBJECT-FIELD-MEASURE (#2)"
TEMPLATE_GEOCODE_BATCH = "GEOCODE-BATCH-COMPARE (#4)"
TEMPLATE_LOCATION_BEARING = "LOCATION-BEARING-CLASSIFY (#5)"
TEMPLATE_PLACE_ATTRIBUTE = "PLACE-ATTRIBUTE-QUERY (#8)"

TEMPLATE_BY_NUMBER = {
    1: TEMPLATE_FILTER_AGGREGATE,
    2: TEMPLATE_OBJECT_FIELD,
    4: TEMPLATE_GEOCODE_BATCH,
    5: TEMPLATE_LOCATION_BEARING,
    8: TEMPLATE_PLACE_ATTRIBUTE,
}

# Concept-transformation / metric strings (mirror train_mapqa_parser.TEMPLATE_MAP)
TEMPLATE_META = {
    TEMPLATE_FILTER_AGGREGATE: (
        "OBJECT(anchor) + SUB_COND(radius) → SUPPORT(within_radius) → MEASURE",
        "Count of amenity entities within a radius",
    ),
    TEMPLATE_OBJECT_FIELD: (
        "OBJECT(a) + OBJECT(b) → FIELD(haversine distance) → MEASURE",
        "Distance in kilometers between two entities",
    ),
    TEMPLATE_GEOCODE_BATCH: (
        "LOCATIONs → coordinates → SUPPORT(distance) × 2 → MEASURE(argmin)",
        "Closer candidate / nearest entity of a given type",
    ),
    TEMPLATE_LOCATION_BEARING: (
        "SUB_COND(direction) + SUPPORT(nearest in direction) → MEASURE",
        "Cardinal direction / nearest entity in a direction cone",
    ),
    TEMPLATE_PLACE_ATTRIBUTE: (
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(adjacency) → MEASURE",
        "Amenity attribute / adjacent entity of a given type",
    ),
}

# Question frames per template (slots: {amenity}, {name}, {a}, {b}, {x}, {y},
# {z}, {r}, {dir}) — reproduces the lexical patterns of the MapQA-llm data
# ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §4).
FRAMES = {
    TEMPLATE_FILTER_AGGREGATE: [
        "What are the {amenity} within {r}m of {name}?",
        "Which {amenity} are within {r}m of {name}?",
        "What {amenity} can be found within {r}m of {name}?",
        "List the {amenity} within {r}m of {name}.",
    ],
    TEMPLATE_OBJECT_FIELD: [
        "How far is {a} from {b}?",
        "What is the distance between {a} and {b}?",
        "How far apart are {a} and {b}?",
    ],
    TEMPLATE_GEOCODE_BATCH: [
        "Which is closer to {z}, {x} or {y}?",
        "Which spot is closer to {z}, {x} or {y}?",
        "What is the nearest {amenity} to {name}?",
        "What is the closest {amenity} around {name}?",
    ],
    TEMPLATE_LOCATION_BEARING: [
        "Which direction is {x} from {y}?",
        "What is the nearest {amenity} {dir} of {name}?",
        "What is the closest {amenity} {dir} of {name}?",
    ],
    TEMPLATE_PLACE_ATTRIBUTE: [
        "What amenity is available at {name}?",
        "What amenity is present in {name}?",
        "What {amenity} is adjacent to {name}?",
        "What {amenity} is right by {name}?",
        "What {amenity} is beside {name}?",
    ],
}

# Radius choices for #1 (meters) — [MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §3
RADII_M = (50, 100, 200, 500)

# 8-way cardinals (full words; matches the executor's N/NE/... sectors)
CARDINALS = ["north", "northeast", "east", "southeast",
             "south", "southwest", "west", "northwest"]

# Per-question-type labels written to the CSV (informational — the parser
# trains on question text + Macro-template, not this column).
QUESTION_TYPES = {
    TEMPLATE_FILTER_AGGREGATE: "self_supervised_radius_count",
    TEMPLATE_OBJECT_FIELD: "self_supervised_distance",
    TEMPLATE_GEOCODE_BATCH: "self_supervised_nearest",
    TEMPLATE_LOCATION_BEARING: "self_supervised_bearing",
    TEMPLATE_PLACE_ATTRIBUTE: "self_supervised_attribute",
}

# Canonical amenity vocabulary fallback (mirrors train_mapqa_parser.OBJECT_SIGNALS)
COMMON_AMENITIES = {
    "bar", "restaurant", "cafe", "hotel", "school", "hospital", "shop",
    "amenity", "pub", "bank", "pharmacy", "park", "church", "library",
    "cinema", "theatre", "gas", "fuel", "parking", "toilet", "atm",
    "bus_station", "fast_food", "place_of_worship", "clinic", "police",
    "post_office", "car_rental", "shelter", "telephone", "bench",
}

# Pool sizing: how many candidate entities to fetch per template before
# sampling with the seeded RNG.
POOL_PER_CLASS_MULTIPLIER = 12
MIN_POOL_SIZE = 120
MAX_POOL_SIZE = 2000

# Distance-range gate for #2 (km) and minimum separation for #5a (m)
MIN_PAIR_DISTANCE_KM = 0.5
MAX_PAIR_DISTANCE_KM = 200.0
MIN_BEARING_PAIR_DISTANCE_M = 100.0

# Direction cone width for #5b (degrees, ± around the cardinal axis)
DIRECTION_CONE_DEGREES = 45
DIRECTION_NEAREST_RADIUS_M = 20000

# Adjacency radius for #8b (plan: 50m)
ADJACENCY_RADIUS_M = 50
