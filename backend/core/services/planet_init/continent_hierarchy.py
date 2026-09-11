"""Canonical seven-continent taxonomy — single source of truth.

Invariant: every raw continent label (hierarchy ``parent_slug``, Geofabrik
top-level ids, Wikidata continent names, legacy underscore/hyphen
variants) normalizes to exactly one of the seven canonical continents, or
``other`` when genuinely unclassified. Display aggregation MUST go through
``normalize_continent`` so Geofabrik subregions (``china``, ``france``,
``united_kingdom``, ``australia``) never surface as continents.

Constraint: the pipeline's *filesystem* hierarchy keeps Geofabrik's
granularity (continent PBFs + subregion PBFs), and the persisted
``OSMWikiDataHierarchy.parent_slug`` is load-bearing for path resolution
(``preprocess_country`` -> ``snapshot_extraction_service`` resolves
``{continent}.pbf``). NEVER rewrite persisted ``parent_slug`` — normalize
at read/display time only.

Mirrors the frozen-dataclass pattern of ``CountryEnvelope`` /
``PlanetEnvelope`` (``pipeline/envelopes.py``). The alias index is derived
from the dataclasses, so there is exactly one source of truth.
"""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class ContinentDefinition:
    """A canonical continent.

    Attributes:
        slug: Canonical identifier used by the display/aggregation layer.
        name: Human display name.
        aliases: Every raw label that may appear as a hierarchy
            ``parent_slug`` and must fold into this continent (Geofabrik
            top-level ids, subregion parents, naming variants).
    """

    slug: str
    name: str
    aliases: Tuple[str, ...] = field(default_factory=tuple)


CONTINENTS: Tuple[ContinentDefinition, ...] = (
    ContinentDefinition("africa", "Africa", ("africa",)),
    ContinentDefinition("antarctica", "Antarctica", ("antarctica",)),
    ContinentDefinition("asia", "Asia", ("asia", "china")),
    ContinentDefinition(
        "europe",
        "Europe",
        ("europe", "france", "united_kingdom", "great-britain", "great_britain"),
    ),
    ContinentDefinition(
        "north-america",
        "North America",
        ("north-america", "north_america", "central-america", "central_america"),
    ),
    ContinentDefinition(
        "oceania",
        "Oceania",
        ("oceania", "australia", "australia-oceania", "australia_oceania"),
    ),
    ContinentDefinition(
        "south-america",
        "South America",
        ("south-america", "south_america"),
    ),
)

CANONICAL_SLUGS = frozenset(c.slug for c in CONTINENTS)

# Derived alias index — the dataclasses are the single source of truth.
_ALIAS_INDEX = {}
for _continent in CONTINENTS:
    for _alias in _continent.aliases:
        _ALIAS_INDEX[_alias] = _continent.slug


def normalize_continent(raw) -> str:
    """Map any raw continent/parent label to a canonical continent slug.

    Normalizes case, whitespace, and space/underscore separators, then
    resolves through the alias index. Unrecognized or empty labels map to
    ``"other"``.
    """
    if not raw:
        return "other"
    key = str(raw).strip().lower().replace(" ", "_")
    return _ALIAS_INDEX.get(key, key if key in CANONICAL_SLUGS else "other")


def continent_name(slug: str) -> str:
    """Human display name for a canonical slug (falls back to the slug)."""
    for continent in CONTINENTS:
        if continent.slug == slug:
            return continent.name
    return slug
