"""Research recipes — plan-type schemas over the shared template/tag vocabulary.

Each recipe declares the criteria (slots), the anchor/radius policy, and
the assembly sections. The loop (decompose → execute → repair →
assemble) is recipe-agnostic; ``route_to_recipe`` picks the recipe from
the prompt; the decompose/assemble prompts are rendered from the recipe.
The repair pipeline reads the recipe's intent vocabulary implicitly via
the shared TAG_RULES / CATEGORY_TAG_TARGETS (``_constants.py``).

First recipe: TRIP_PLAN. Further recipes (e.g. RESTAURANT_MATCH — "find
restaurants like X") plug into the same engine: a recipe row + a router
pattern, no loop changes.
"""

import re

# ── Registry ─────────────────────────────────────────────────────────────

RECIPES = {}


def _register(recipe):
    RECIPES[recipe["key"]] = recipe
    return recipe


TRIP_PLAN = {
    "key": "trip_plan",
    "label": "trip plan",
    # The plan's criteria: each slot is one section of the final summary.
    # The planner assigns one or more questions per slot.
    "slots": [
        {"id": "stay", "section": "Where to stay"},
        {"id": "eat", "section": "Where to eat"},
        {"id": "see", "section": "What to see"},
        {"id": "move", "section": "Getting around"},
        {"id": "choose", "section": "Choose between", "optional": True},
        {"id": "interest", "section": "Interests", "optional": True},
    ],
    "radius_policy": (
        "Scale the radius to the anchor's extent: a city-scale anchor "
        "(Dublin, Seoul, Rome) needs at least 2 km, prefer 3-5 km; a "
        "venue, building, or neighbourhood anchor works at 500m-2km. "
        "Never use 1 km for a city."
    ),
    "anchor_policy": (
        "Anchor the plan at ONE destination: base the stay, eat, and see "
        "slots on the destination. When a slot needs another region (an "
        "excursion), pair it with a distance slot between the destination "
        "and that region so the plan stays geographically connected."
    ),
    "assemble_sections": (
        "where to stay", "where to eat", "what to see", "getting around",
    ),
}

_register(TRIP_PLAN)

DEFAULT_RECIPE_KEY = "trip_plan"


def get_recipe(key: str = None) -> dict:
    """The recipe for a key (default: TRIP_PLAN)."""
    return RECIPES.get(key or DEFAULT_RECIPE_KEY, TRIP_PLAN)


# ── Router ───────────────────────────────────────────────────────────────
# Deterministic-first, closed-set: each pattern maps the prompt to a
# recipe key. When a second recipe lands, an ambiguous prompt gets ONE
# chat_json over the closed set of recipe keys (same gate discipline as
# the template manifest) — never an open-set label.

_ROUTER_PATTERNS = [
    (
        re.compile(
            r"\b(plan|planning)\b|\b(trip|itinerary|vacation|travel|"
            r"visit|getaway|holiday|tour)\b|\bday(s)?\b",
            re.IGNORECASE,
        ),
        "trip_plan",
    ),
]


def route_to_recipe(prompt: str = None) -> dict:
    """Pick the recipe for a research prompt (default TRIP_PLAN)."""
    for pattern, key in _ROUTER_PATTERNS:
        if pattern.search(prompt or ""):
            return get_recipe(key)
    return get_recipe(DEFAULT_RECIPE_KEY)
