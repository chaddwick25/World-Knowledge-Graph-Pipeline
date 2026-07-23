import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from django.conf import settings


logger = logging.getLogger(__name__)


_OVERRIDES_CACHE: Optional[Dict[str, Any]] = None


def _get_overrides_path() -> Optional[Path]:
    """Resolve the overrides.json path from settings.

    Uses OVERRIDES_JSON_PATH, which is already configured to point at cold storage
    (e.g. /media/.../OSM/overrides.json) via settings.COLD_STORAGE_BASE_DIR.
    """
    path_str = getattr(settings, "OVERRIDES_JSON_PATH", None)
    if not path_str:
        return None
    return Path(path_str)


def load_overrides() -> Dict[str, Any]:
    """Load overrides.json once and cache it.

    Supports two shapes:
    - Flat legacy mapping (e.g. {"IE": {"country_slug": "ireland"}, ...}).
    - Structured mapping with sections, e.g. {
        "continents": {...},
        "country_iso_overrides": {...},
        "embedding_edge_cases": {...}
      }.
    """
    global _OVERRIDES_CACHE

    if _OVERRIDES_CACHE is not None:
        return _OVERRIDES_CACHE

    path = _get_overrides_path()
    if not path:
        logger.info("OVERRIDES_JSON_PATH is not configured; proceeding without overrides.")
        _OVERRIDES_CACHE = {}
        return _OVERRIDES_CACHE

    if not path.exists():
        logger.info("overrides.json not found at %s; proceeding without overrides.", path)
        _OVERRIDES_CACHE = {}
        return _OVERRIDES_CACHE

    try:
        with path.open("r") as f:
            data = json.load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load overrides.json from %s: %s", path, exc)
        data = {}

    # Ensure we always have dicts for known sections when using structured schema
    if isinstance(data, dict):
        if any(k in data for k in ("continents", "country_iso_overrides", "embedding_edge_cases")):
            data.setdefault("continents", {})
            data.setdefault("country_iso_overrides", {})
            data.setdefault("embedding_edge_cases", {})

    _OVERRIDES_CACHE = data
    return _OVERRIDES_CACHE


def get_country_override_record(iso: str) -> Optional[Dict[str, Any]]:
    """Return the override record for a given ISO/QID, if present.

    Looks in the "country_iso_overrides" section first (structured schema),
    then falls back to flat top-level keys for legacy JSON.
    """
    if not iso:
        return None

    iso_key = str(iso).upper()
    data = load_overrides()
    if not isinstance(data, dict) or not data:
        return None

    # Structured schema: explicit country_iso_overrides section
    section = data.get("country_iso_overrides")
    if isinstance(section, dict) and section:
        # Case-insensitive lookup on keys
        for key, record in section.items():
            try:
                if str(key).upper() == iso_key:
                    return record if isinstance(record, dict) else None
            except Exception:
                continue

    # Legacy schema: treat top-level keys as ISO/QID
    for key, record in data.items():
        # Skip known structured sections
        if key in ("continents", "country_iso_overrides", "embedding_edge_cases"):
            continue
        try:
            if str(key).upper() == iso_key:
                # If the record is already a dict, return as-is. If it's a bare
                # string (e.g. {"IE": "ireland"}), interpret it as
                # country_slug for backward compatibility.
                if isinstance(record, dict):
                    return record
                return {"country_slug": str(record)}
        except Exception:
            continue

    return None


def get_country_slug(iso: Optional[str], default_slug: str) -> str:
    """Return the override country slug for an ISO, falling back to default_slug.

    The override record may use either "country_slug" or a more generic "slug"
    field; we check both in that order.
    """
    if not iso:
        return default_slug

    record = get_country_override_record(iso)
    if not record:
        return default_slug

    slug = record.get("country_slug") or record.get("slug")
    if not slug:
        return default_slug
    return str(slug)


def get_continent_override(name: str) -> Optional[Dict[str, Any]]:
    """Return a continent override record keyed by embedding/folder name.

    Looks in the "continents" section when present. Keys are treated
    case-insensitively.
    """
    if not name:
        return None

    data = load_overrides()
    if not isinstance(data, dict):
        return None

    section = data.get("continents")
    if not isinstance(section, dict) or not section:
        return None

    target = str(name).lower()
    for key, record in section.items():
        try:
            if str(key).lower() == target:
                return record if isinstance(record, dict) else None
        except Exception:
            continue

    return None


def get_embedding_edge_case(name: str) -> Optional[Dict[str, Any]]:
    """Return an embedding edge-case record for a given embedding slug.

    This section is optional and is intended to document special relationships
    such as shared locations or alternate tag views (e.g. US-Other vs US-West).
    """
    if not name:
        return None

    data = load_overrides()
    if not isinstance(data, dict):
        return None

    section = data.get("embedding_edge_cases")
    if not isinstance(section, dict) or not section:
        return None

    target = str(name).lower()
    for key, record in section.items():
        try:
            if str(key).lower() == target:
                return record if isinstance(record, dict) else None
        except Exception:
            continue

    return None


def clear_cache() -> None:
    """Clear the in-memory overrides cache (useful for tests)."""
    global _OVERRIDES_CACHE
    _OVERRIDES_CACHE = None
