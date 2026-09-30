"""
Non-Sovereign Territories Registry

Maps non-sovereign countries/territories (Wales, Scotland, England, crown
dependencies, overseas territories, etc.) to **synthetic 2-letter ISO-like codes**,
enabling them to participate in the pipeline even though they have no official
ISO 3166-1 alpha-2 code.

Convention
----------
Synthetic codes use two uppercase letters that are NOT assigned by ISO 3166-1.
The codes are:
  - Chosen to be mnemonic where possible (WL → Wales)
  - Guaranteed unique within this registry
  - NEVER written to external systems that expect real ISO codes
  - Used only internally as DB/identifier keys

Usage
-----
  from core.services.planet_init.non_sovereign_territories import (
      is_non_sovereign_synthetic_iso,
      resolve_non_sovereign_iso,
      NON_SOVEREIGN_TERRITORIES,
      synthetic_iso_to_info,
  )

  # Check if a name/slug maps to a synthetic ISO
  iso = resolve_non_sovereign_iso("wales")  # Returns "WL"
  iso = resolve_non_sovereign_iso("scotland")  # Returns "SC"

  # Check if a code is synthetic
  is_synthetic = is_non_sovereign_synthetic_iso("WL")  # True
  is_synthetic = is_non_sovereign_synthetic_iso("GB")  # False

  # Get metadata for a synthetic code
  info = synthetic_iso_to_info("WL")  # Returns {name, slug, continent, ...}
"""

from typing import Dict, Optional, Union


# ── Registry of non-sovereign territories ──────────────────────────────
#
# Each entry maps a synthetic 2-letter code to:
#   - name:         Display name (e.g., "Wales")
#   - slug:         Filesystem slug (e.g., "wales")
#   - continent:    Continent slug (e.g., "europe")
#   - parent_iso:   ISO code of the sovereign state (if applicable)
#   - wikidata_qid: Wikidata QID if known (for future enrichment)
#   - osm_relation_id: OSM relation ID if known (for future enrichment)
#
NON_SOVEREIGN_TERRITORIES: Dict[str, Dict[str, Optional[Union[str, int]]]] = {
    # Great Britain split (from step_0i_prebuild_split_embeddings)
    "WL": {  # Wales — OSM relation 58437
        "name": "Wales",
        "slug": "wales",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q25",
        "osm_relation_id": 58437,
    },
    "XS": {  # Scotland — OSM relation 58446
        "name": "Scotland",
        "slug": "scotland",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q22",
        "osm_relation_id": 58446,
    },
    "EN": {  # England — OSM relation 58447
        "name": "England",
        "slug": "england",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q21",
        "osm_relation_id": 58447,
    },
    # Crown dependencies (orphan TSVs that exist on disk)
    "IM": {  # Isle of Man — OSM relation 62269
        "name": "Isle of Man",
        "slug": "im",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q9676",
        "osm_relation_id": 62269,
    },
    "GG": {  # Guernsey — OSM relation 270747
        "name": "Guernsey",
        "slug": "gg",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q423",
        "osm_relation_id": 270747,
    },
    "JE": {  # Jersey — OSM relation 270747 (same relation as Guernsey)
        "name": "Jersey",
        "slug": "je",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q270747",
        "osm_relation_id": 270747,
    },
    # Future additions can be added here:
    # "KO": {  # Kosovo
    #     "name": "Kosovo",
    #     "slug": "kosovo",
    #     "continent": "europe-east",
    #     "parent_iso": None,  # Disputed territory
    #     "wikidata_qid": "Q1246",
    #     "osm_relation_id": None,
    # },
}

# ── Reverse mappings ───────────────────────────────────────────────────

# slug → synthetic ISO
_SLUG_TO_SYNTHETIC_ISO: Dict[str, str] = {
    info["slug"]: code
    for code, info in NON_SOVEREIGN_TERRITORIES.items()
}

# name (lowercase) → synthetic ISO
_NAME_TO_SYNTHETIC_ISO: Dict[str, str] = {
    info["name"].lower(): code
    for code, info in NON_SOVEREIGN_TERRITORIES.items()
}

# Set of all synthetic ISO codes for fast membership tests
_SYNTHETIC_ISO_CODES = set(NON_SOVEREIGN_TERRITORIES.keys())


# ── Public API ─────────────────────────────────────────────────────────


def is_non_sovereign_synthetic_iso(code: str) -> bool:
    """Check if a 2-letter code is a synthetic non-sovereign ISO."""
    return code.upper() in _SYNTHETIC_ISO_CODES


def resolve_non_sovereign_iso(name_or_slug: str) -> Optional[str]:
    """
    Resolve a country name or slug to a synthetic ISO code.

    Tries slug match first, then full name match (case-insensitive).

    Args:
        name_or_slug: e.g. "wales", "Wales", "scotland", "Scotland"

    Returns:
        Synthetic 2-letter ISO code (e.g. "WL", "SC"), or None if not found.
    """
    if not name_or_slug:
        return None

    cleaned = name_or_slug.strip().lower()

    # Try slug match
    if cleaned in _SLUG_TO_SYNTHETIC_ISO:
        return _SLUG_TO_SYNTHETIC_ISO[cleaned]

    # Try name match
    if cleaned in _NAME_TO_SYNTHETIC_ISO:
        return _NAME_TO_SYNTHETIC_ISO[cleaned]

    return None


def synthetic_iso_to_info(code: str) -> Optional[Dict[str, Optional[Union[str, int]]]]:
    """Get metadata for a synthetic ISO code.

    Returns None if the code is not a known synthetic ISO.
    """
    return NON_SOVEREIGN_TERRITORIES.get(code.upper())


def get_all_synthetic_isos() -> Dict[str, Dict[str, Optional[Union[str, int]]]]:
    """Return the full registry of non-sovereign territories.

    Returns a copy to prevent mutation of the canonical registry.
    """
    return dict(NON_SOVEREIGN_TERRITORIES)
