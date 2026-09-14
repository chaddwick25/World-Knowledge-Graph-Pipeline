"""
EntityGeocoder — resolve entity names to OsmEntity objects with lat/lon.

Uses PostGIS ILIKE name search first (fast, exact-ish), falls back to
FastText semantic search on the name (handles "Hollywood Boulevard" vs
"Hollywood Blvd").

Implements MAPQA_TO_EXECUTION_PLAN.md §2.2.3.
"""

import logging
import math

from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)


class EntityGeocoder:
    """Resolve entity names to coordinates using the OsmEntity table."""

    @staticmethod
    def geocode(entity_name: str, country_code: str = None,
                snapshot_date: str = None) -> dict:
        """Resolve an entity name to {osm_id, name, lat, lon, tags}.

        Args:
            entity_name: The entity name to geocode (e.g. "Hollywood Blvd")
            country_code: Optional ISO 3166-1 alpha-2 code to scope the search
            snapshot_date: Optional YYYY_MM_DD snapshot partition key

        Returns:
            dict with osm_id, name, lat, lon, tags, or None if not found.
        """
        if not entity_name:
            return None

        snapshot_id = snapshot_date or get_latest_snapshot_id()
        if not snapshot_id:
            logger.warning("No snapshot_id available for geocoding")
            return None

        ***REMOVED*** Strip leading articles ("a bus station" → "bus station")
        clean_name = EntityGeocoder._strip_articles(entity_name)

        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        ***REMOVED*** The cleaned name usually equals the original — dedupe so we never
        ***REMOVED*** run the same (potentially expensive) fallback query twice.
        variants = [v for v in dict.fromkeys([clean_name, entity_name]) if v]

        ***REMOVED*** 1. Fuzzy match via pg_trgm similarity on the romanized name
        ***REMOVED***    (GIN trigram index — ~40ms even on misses, tolerant of typos:
        ***REMOVED***    "Shannon Bells" → "Shandon Bells" at sim 0.65).  Runs FIRST:
        ***REMOVED***    the unindexed tags->>'name' scans below cost ~4-9s on a miss
        ***REMOVED***    (IE leaf ~9.7M rows), while an exact-match hit always has
        ***REMOVED***    similarity 1.0, so the trigram ranking returns the same entity
        ***REMOVED***    for correctly-spelled romanized names.
        for name_variant in variants:
            try:
                ***REMOVED*** The `%` operator is index-assisted; Django's
                ***REMOVED*** `__trigram_similar` lookup is TextField-only, so use the
                ***REMOVED*** raw operator + similarity() ranking directly.  `%%` is
                ***REMOVED*** psycopg2's escape for a literal `%` — a bare `%` collides
                ***REMOVED*** with placeholder parsing (IndexError).  The explicit
                ***REMOVED*** similarity >= 0.4 guard rejects junk matches (the default
                ***REMOVED*** 0.3 threshold false-positives, e.g. "paranormal nonsense
                ***REMOVED*** place" → "Paradise Place" at ~0.32).
                from django.contrib.postgres.search import TrigramSimilarity
                ***REMOVED*** Fragment guard: a short query ("Cliffs") must not match a
                ***REMOVED*** much longer candidate ("Cliffs of Howth") at low similarity
                ***REMOVED*** — that is a truncated entity span, not a typo. Preserves
                ***REMOVED*** the equal-length typo case ("Shannon Bells" → "Shandon
                ***REMOVED*** Bells", sim 0.65).
                qs_fuzzy = (
                    qs.extra(
                        where=[
                            "name_romanized %% %s AND "
                            "similarity(name_romanized, %s) >= 0.4 AND "
                            "NOT (similarity(name_romanized, %s) < 0.6 AND "
                            "     length(name_romanized) > %s)"
                        ],
                        params=[name_variant, name_variant, name_variant,
                                int(1.6 * len(name_variant))],
                    )
                    .order_by(TrigramSimilarity("name_romanized", name_variant).desc())
                )
                entity = qs_fuzzy.first()
                if entity:
                    result = EntityGeocoder._entity_to_dict(entity)
                    if result and result.get("lat") is not None:
                        return result
            except Exception:  ***REMOVED*** noqa: BLE001 — trigram unavailable → skip
                break

        ***REMOVED*** 2. Exact name match (case-insensitive) — check the 'name' tag
        for name_variant in variants:
            qs_exact = qs.filter(tags__name__iexact=name_variant)
            entity = qs_exact.first()
            if entity:
                result = EntityGeocoder._entity_to_dict(entity)
                if result and result.get("lat") is not None:
                    return result
                ***REMOVED*** Exact match but no coords — keep looking but remember as fallback
                fallback = result

        ***REMOVED*** 3. ILIKE contains match — prefer entities with valid coordinates.
        ***REMOVED***    Unbounded scan on names not romanized (94% of IE) — only hit
        ***REMOVED***    when both the indexed trigram and exact steps missed.
        for name_variant in variants:
            qs_ilike = qs.filter(tags__name__icontains=name_variant)
            ***REMOVED*** Fragment guard (Kuhn's Template correction layer): a short
            ***REMOVED*** query ("Cliffs", 6 chars) must not substring-match a much
            ***REMOVED*** longer name ("Cliffs of Howth") — that is a truncated span,
            ***REMOVED*** not a partial name. Partial-name queries of >= 8 chars and
            ***REMOVED*** contained names up to 2x the query length are unaffected.
            if len(name_variant) < 8:
                qs_ilike = qs_ilike.extra(
                    where=["length(tags->>'name') <= %s"],
                    params=[2 * len(name_variant)],
                )
            ***REMOVED*** Prefer nodes (which have valid Point geom) over ways
            for entity in qs_ilike[:20]:
                result = EntityGeocoder._entity_to_dict(entity)
                if result and result.get("lat") is not None:
                    return result
                fallback = result

        ***REMOVED*** 4. Try with common abbreviations expanded
        expanded = EntityGeocoder._expand_abbreviations(clean_name)
        if expanded != clean_name:
            qs_exp = qs.filter(tags__name__icontains=expanded)
            for entity in qs_exp[:20]:
                result = EntityGeocoder._entity_to_dict(entity)
                if result and result.get("lat") is not None:
                    return result
                fallback = result

        ***REMOVED*** 5. Last resort: return the best match even without coordinates
        ***REMOVED***    (the executor may still use it as a graph node lookup)
        try:
            fallback
        except NameError:
            fallback = None
        if fallback:
            return fallback

        logger.debug("Geocode failed for '%s' (country=%s, snapshot=%s)",
                     entity_name, country_code, snapshot_id)
        return None

    @staticmethod
    def geocode_many(entity_names: list, country_code: str = None,
                     snapshot_date: str = None) -> list:
        """Geocode multiple entity names. Returns list of dicts or None."""
        return [
            EntityGeocoder.geocode(name, country_code, snapshot_date)
            for name in entity_names
        ]

    @staticmethod
    def _entity_to_dict(entity: OsmEntity) -> dict:
        tags = entity.tags or {}
        lat = lon = None
        if entity.geom:
            try:
                y = entity.geom.y
                x = entity.geom.x
                ***REMOVED*** Ways may have POINT(NaN NaN) — treat as no coordinates
                if y is not None and x is not None and not (
                    isinstance(y, float) and math.isnan(y)
                ) and not (
                    isinstance(x, float) and math.isnan(x)
                ):
                    lat = y
                    lon = x
            except Exception:
                pass
            if lat is None:
                ***REMOVED*** Ways/relations have line/polygon geometries — .x/.y yield
                ***REMOVED*** nothing, so derive a representative point. Without this,
                ***REMOVED*** entities like "Spire of Dublin" (a way) geocode to
                ***REMOVED*** lat=None and every coordinate-dependent executor path
                ***REMOVED*** (cone search, distance, anchors) fails on them.
                try:
                    pt = entity.geom.point_on_surface
                    if pt is not None:
                        ly = pt.y
                        lx = pt.x
                        if (
                            ly is not None and lx is not None
                            and not (isinstance(ly, float) and math.isnan(ly))
                            and not (isinstance(lx, float) and math.isnan(lx))
                        ):
                            lat = ly
                            lon = lx
                except Exception:
                    pass
        return {
            "osm_id": entity.osm_id,
            "osm_type": entity.osm_type,
            "name": tags.get("name", ""),
            "lat": lat,
            "lon": lon,
            "tags": tags,
            "wkg_class": entity.wkg_class,
            ***REMOVED*** Lets the executor prune context queries to the entity's leaf
            ***REMOVED*** partition instead of Appending over every country.
            "country_code": entity.country_code,
        }

    @staticmethod
    def _strip_articles(name: str) -> str:
        """Strip leading English articles: 'a bus station' → 'bus station'."""
        if not name:
            return name
        lower = name.lower().strip()
        for article in ("a ", "an ", "the "):
            if lower.startswith(article):
                return name.strip()[len(article):].strip()
        return name.strip()

    @staticmethod
    def _expand_abbreviations(name: str) -> str:
        """Expand common street/avenue abbreviations."""
        replacements = {
            " blvd": " boulevard",
            " ave": " avenue",
            " st": " street",
            " rd": " road",
            " dr": " drive",
            " ln": " lane",
            " hwy": " highway",
            " fwy": " freeway",
            " expy": " expressway",
            " pkwy": " parkway",
            " pl": " place",
            " sq": " square",
            " ct": " court",
        }
        lower = name.lower()
        for abbrev, full in replacements.items():
            if lower.endswith(abbrev):
                return name[:len(name) - len(abbrev)] + full
        return name
