"""Research recipes — plan-type schemas over the shared template/tag vocabulary.

Each recipe declares the criteria (slots), the anchor/radius policy, and
the assembly sections. The loop (decompose → execute → repair →
assemble) is recipe-agnostic; ``route_to_recipe`` picks the recipe from
the prompt; the decompose/assemble prompts are rendered from the recipe.
The repair pipeline reads the recipe's intent vocabulary implicitly via
the shared TAG_RULES / CATEGORY_TAG_TARGETS (``_constants.py``).

The only recipe today: FOOD_REPORT — a food & drink vendor report
(restaurants, cafes, bars, pubs, food trucks, street vendors, vending
machines…), scoped by design: shops, services, health, transit, and
infrastructure are out. Sections: where to eat and drink, popular
brands (derived name-frequency tally), local vs international cuisine
(cuisine-tag split), and getting around measured as distances/clustering
between food spots. The PLACE_REPORT recipe was replaced 2026-10-02 —
the owner wanted one focused recipe, not a domain-neutral place survey.
Further recipes plug into the same engine: a recipe row + a router
pattern, no loop changes.
"""

# ── Registry ─────────────────────────────────────────────────────────────

RECIPES = {}


def _register(recipe):
    RECIPES[recipe["key"]] = recipe
    return recipe


FOOD_REPORT = {
    "key": "food_report",
    "label": "food & drink report",
    # The plan's criteria: each slot is one section of the final summary.
    # Derived slots carry no question — the section is computed from the
    # other slots' results (the brands tally is a name-frequency count,
    # not an LLM guess). Food & drink vendors only: shops, services,
    # health, transit, and infrastructure are out of scope by design
    # (the "getting around" slot measures travel BETWEEN food spots —
    # distances and clustering — not transport).
    "slots": [
        {"id": "eat", "section": "Where to eat and drink"},
        {"id": "brands", "section": "Popular brands", "derived": True},
        {"id": "cuisine", "section": "Local vs international cuisine"},
        {"id": "move", "section": "Getting around between food spots"},
    ],
    # The planner's resolvable class vocabulary — food & drink vendors
    # only (each maps to an amenity=[value] or cuisine family in
    # CATEGORY_TAG_TARGETS/TAG_RULES).
    "class_vocabulary": (
        "restaurants, cafes, fast food, bars, pubs, taverns, beer "
        "gardens, nightclubs, food courts, food trucks, street food, "
        "snack bars, ice cream, vending machines, bakeries"
    ),
    "radius_policy": (
        "YOU decide each question's radius (output radius_m) from the "
        "entity class and the anchor's extent — do not copy a template "
        "radius. Class-aware bands: a city-scale anchor (Seoul, "
        "Belmopan) needs at least 2 km, prefer 3-20 km for vendor "
        "searches across a city; a venue, building, or neighbourhood "
        "anchor works at 300m-2km; a 'nearest' question stays under "
        "2 km. Never use 1 km for a city. Never exceed 50 km. State "
        "the radius in the question text AND as radius_m."
    ),
    "anchor_policy": (
        "Anchor every question at a named city or town (they geocode to "
        "real centroids). Cover the country by picking its major cities "
        "— one vendor-radius question per city — plus venue-scale "
        "questions around the top entities the city questions surface."
    ),
    "assemble_sections": (
        "where to eat and drink", "popular brands",
        "local vs international cuisine",
        "getting around between food spots",
    ),
    # Notes the assemble prompt appends verbatim — the brands/cuisine
    # tallies are computed counts, so the LLM cites them and never
    # estimates.
    "assemble_notes": (
        " For popular brands, use the derived brand tally — names "
        "repeated across results are the chains; never estimate counts. "
        "For cuisine, the cuisine-tag tally already separates local from "
        "international — you may name the country's famous traditional "
        "dishes as context next to the local-cuisine findings. Getting "
        "around is between food spots only: describe how clustered the "
        "cafes and restaurants are and how far apart the top picks sit "
        "— never mention transit, fuel, or infrastructure."
    ),
    # Deterministic fill for required slots the planner skipped: the
    # decompose validation guarantees every required (non-derived) slot
    # has >= 1 question — these resolve through the parser's vocabulary
    # ({anchor} is the first question's anchor). cuisine/move reuse the
    # vendor radius question — the cuisine census and cluster read are
    # computed from the results, not a special template.
    "slot_defaults": {
        "eat": "Which restaurants are within 10km of {anchor}?",
        "cuisine": "Which restaurants are within 10km of {anchor}?",
        "move": "Which cafes are within 1km of {anchor}?",
    },
}

_register(FOOD_REPORT)

DEFAULT_RECIPE_KEY = "food_report"


def get_recipe(key: str = None) -> dict:
    """The recipe for a key (default: FOOD_REPORT)."""
    return RECIPES.get(key or DEFAULT_RECIPE_KEY, FOOD_REPORT)


# ── Router ───────────────────────────────────────────────────────────────
# Deterministic-first, closed-set: each pattern maps the prompt to a
# recipe key; an ambiguous prompt gets ONE chat_json over the closed set
# (same gate discipline as the template manifest) — never an open-set
# label. No second recipe today, so every prompt stays on the default;
# when one lands, add a pattern here + a recipe row — the loop, repair
# pipeline, and SSE spine stay unchanged.

_ROUTER_PATTERNS = []


def route_to_recipe(prompt: str = None) -> dict:
    """Pick the recipe for a research prompt (default FOOD_REPORT)."""
    for pattern, key in _ROUTER_PATTERNS:
        if pattern.search(prompt or ""):
            return get_recipe(key)
    return get_recipe(DEFAULT_RECIPE_KEY)
