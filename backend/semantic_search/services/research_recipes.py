"""Research recipes — plan-type schemas over the shared template/tag vocabulary.

Each recipe declares the criteria (slots), the anchor/radius policy, and
the assembly sections. The loop (decompose → execute → repair →
assemble) is recipe-agnostic; ``route_to_recipe`` picks the recipe from
the prompt; the decompose/assemble prompts are rendered from the recipe.
The repair pipeline reads the recipe's intent vocabulary implicitly via
the shared TAG_RULES / CATEGORY_TAG_TARGETS (``_constants.py``).

The only recipe today: PLACE_REPORT — a domain-neutral "what exists here"
report over the classes OSM maps most completely (commercial entities,
services, transit, infrastructure). The TRIP_PLAN recipe was removed
2026-10-01 (tourism strained the data; the general researcher leads with
data-supported domains). Further recipes plug into the same engine: a
recipe row + a router pattern, no loop changes.
"""

# ── Registry ─────────────────────────────────────────────────────────────

RECIPES = {}


def _register(recipe):
    RECIPES[recipe["key"]] = recipe
    return recipe


PLACE_REPORT = {
    "key": "place_report",
    "label": "place report",
    # The plan's criteria: each slot is one section of the final summary.
    # The planner assigns one or more questions per slot. Slots whose
    # target classes are transport infrastructure (bus stops, transit,
    # rail) set ``transport_evidence`` — the evidence filter excludes
    # those from probe digests by default (abundant-but-noise for a
    # "hikes" slot); for a transit slot the abundance IS the signal.
    "slots": [
        {"id": "eat", "section": "Where to eat and drink"},
        {"id": "shop", "section": "Where to shop"},
        {"id": "services", "section": "Services and health"},
        {"id": "move", "section": "Getting around", "transport_evidence": True},
        {"id": "infra", "section": "Infrastructure"},
        {"id": "interest", "section": "Interests", "optional": True},
    ],
    "radius_policy": (
        "YOU decide each question's radius (output radius_m) from the "
        "entity class and the anchor's extent — do not copy a template "
        "radius. Class-aware bands: a city-scale anchor (Belmopan, Mexico "
        "City) needs at least 2 km, prefer 3-10 km for food/shop/service "
        "searches; a venue, building, or neighbourhood anchor works at "
        "300m-2km; transit and infrastructure (bus stops, fuel stations, "
        "charging) are dense — 500m-2km around a hub; a 'nearest' "
        "question stays under 2 km. Never use 1 km for a city. Never "
        "exceed 50 km. State the radius in the question text AND as "
        "radius_m."
    ),
    "anchor_policy": (
        "Anchor the report at ONE settlement or hub: base the eat, shop, "
        "services, and transit slots on it. When a slot needs another "
        "area, pair it with a distance slot between the settlement and "
        "that area so the report stays geographically connected."
    ),
    "assemble_sections": (
        "where to eat and drink", "where to shop", "services and health",
        "getting around", "infrastructure",
    ),
    # Deterministic fill for required slots the planner skipped: the
    # decompose validation guarantees every required slot has >= 1
    # question — these resolve through the parser's vocabulary
    # ({anchor} is the first question's anchor).
    "slot_defaults": {
        "eat": "Which restaurants are within 3km of {anchor}?",
        "shop": "Which shops are within 3km of {anchor}?",
        "services": "What services are near {anchor}?",
        "move": "Which bus stops are near {anchor}?",
        "infra": "Which fuel stations are within 3km of {anchor}?",
        "interest": "What is around {anchor}?",
    },
}

_register(PLACE_REPORT)

DEFAULT_RECIPE_KEY = "place_report"


def get_recipe(key: str = None) -> dict:
    """The recipe for a key (default: PLACE_REPORT)."""
    return RECIPES.get(key or DEFAULT_RECIPE_KEY, PLACE_REPORT)


# ── Router ───────────────────────────────────────────────────────────────
# Deterministic-first, closed-set: each pattern maps the prompt to a
# recipe key; an ambiguous prompt gets ONE chat_json over the closed set
# (same gate discipline as the template manifest) — never an open-set
# label. No second recipe today, so every prompt stays on the default;
# when one lands, add a pattern here + a recipe row — the loop, repair
# pipeline, and SSE spine stay unchanged.

_ROUTER_PATTERNS = []


def route_to_recipe(prompt: str = None) -> dict:
    """Pick the recipe for a research prompt (default PLACE_REPORT)."""
    for pattern, key in _ROUTER_PATTERNS:
        if pattern.search(prompt or ""):
            return get_recipe(key)
    return get_recipe(DEFAULT_RECIPE_KEY)
