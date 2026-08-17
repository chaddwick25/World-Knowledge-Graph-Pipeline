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

  ***REMOVED*** Check if a name/slug maps to a synthetic ISO
  iso = resolve_non_sovereign_iso("wales")  ***REMOVED*** Returns "WL"
  iso = resolve_non_sovereign_iso("scotland")  ***REMOVED*** Returns "SC"

  ***REMOVED*** Check if a code is synthetic
  is_synthetic = is_non_sovereign_synthetic_iso("WL")  ***REMOVED*** True
  is_synthetic = is_non_sovereign_synthetic_iso("GB")  ***REMOVED*** False

  ***REMOVED*** Get metadata for a synthetic code
  info = synthetic_iso_to_info("WL")  ***REMOVED*** Returns {name, slug, continent, ...}
"""

from typing import Dict, Optional, Union


***REMOVED*** ── Registry of non-sovereign territories ──────────────────────────────
***REMOVED***
***REMOVED*** Each entry maps a synthetic 2-letter code to:
***REMOVED***   - name:         Display name (e.g., "Wales")
***REMOVED***   - slug:         Filesystem slug (e.g., "wales")
***REMOVED***   - continent:    Continent slug (e.g., "europe")
***REMOVED***   - parent_iso:   ISO code of the sovereign state (if applicable)
***REMOVED***   - wikidata_qid: Wikidata QID if known (for future enrichment)
***REMOVED***   - osm_relation_id: OSM relation ID if known (for future enrichment)
***REMOVED***
NON_SOVEREIGN_TERRITORIES: Dict[str, Dict[str, Optional[Union[str, int]]]] = {
    ***REMOVED*** Great Britain split (from step_0i_prebuild_split_embeddings)
    "WL": {  ***REMOVED*** Wales — OSM relation 58437
        "name": "Wales",
        "slug": "wales",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q25",
        "osm_relation_id": 58437,
    },
    "XS": {  ***REMOVED*** Scotland — OSM relation 58446
        "name": "Scotland",
        "slug": "scotland",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q22",
        "osm_relation_id": 58446,
    },
    "EN": {  ***REMOVED*** England — OSM relation 58447
        "name": "England",
        "slug": "england",
        "continent": "europe",
        "parent_iso": "GB",
        "wikidata_qid": "Q21",
        "osm_relation_id": 58447,
    },
    ***REMOVED*** Crown dependencies (orphan TSVs that exist on disk)
    "IM": {  ***REMOVED*** Isle of Man — OSM relation 62269
        "name": "Isle of Man",
        "slug": "im",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q9676",
        "osm_relation_id": 62269,
    },
    "GG": {  ***REMOVED*** Guernsey — OSM relation 270747
        "name": "Guernsey",
        "slug": "gg",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q423",
        "osm_relation_id": 270747,
    },
    "JE": {  ***REMOVED*** Jersey — OSM relation 270747 (same relation as Guernsey)
        "name": "Jersey",
        "slug": "je",
        "continent": "europe-west",
        "parent_iso": "GB",
        "wikidata_qid": "Q270747",
        "osm_relation_id": 270747,
    },
    ***REMOVED*** Future additions can be added here:
    ***REMOVED*** "KO": {  ***REMOVED*** Kosovo
    ***REMOVED***     "name": "Kosovo",
    ***REMOVED***     "slug": "kosovo",
    ***REMOVED***     "continent": "europe-east",
    ***REMOVED***     "parent_iso": None,  ***REMOVED*** Disputed territory
    ***REMOVED***     "wikidata_qid": "Q1246",
    ***REMOVED***     "osm_relation_id": None,
    ***REMOVED*** },
}

***REMOVED*** ── Reverse mappings ───────────────────────────────────────────────────

***REMOVED*** slug → synthetic ISO
_SLUG_TO_SYNTHETIC_ISO: Dict[str, str] = {
    info["slug"]: code
    for code, info in NON_SOVEREIGN_TERRITORIES.items()
}

***REMOVED*** name (lowercase) → synthetic ISO
_NAME_TO_SYNTHETIC_ISO: Dict[str, str] = {
    info["name"].lower(): code
    for code, info in NON_SOVEREIGN_TERRITORIES.items()
}

***REMOVED*** Set of all synthetic ISO codes for fast membership tests
_SYNTHETIC_ISO_CODES = set(NON_SOVEREIGN_TERRITORIES.keys())


***REMOVED*** ── Public API ─────────────────────────────────────────────────────────


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

    ***REMOVED*** Try slug match
    if cleaned in _SLUG_TO_SYNTHETIC_ISO:
        return _SLUG_TO_SYNTHETIC_ISO[cleaned]

    ***REMOVED*** Try name match
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
