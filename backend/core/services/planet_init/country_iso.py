"""ISO 3166-1 alpha-2 resolution.

Extracted from ``country_metadata.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from typing import Optional

from core.models import OSMWikiDataHierarchy
from core.services.planet_init.country_relations import (
    get_country_by_name,
    get_country_relations_dict,
)


def resolve_iso_code(input_code: str) -> str:

    """Resolve a QID or country name to a real ISO 3166-1 alpha-2 code.



    Handles three input forms:

      - QID (e.g. 'Q27')          -> looks up OSMWikiDataHierarchy.wikidata_id

      - Country name (e.g. 'Ireland', 'IRELAND') -> looks up via get_country_by_name

      - Synthetic ISO (e.g. 'WL') -> returns as-is (already a valid synthetic code)

      - Real ISO (e.g. 'IE')      -> returns as-is (no resolution needed)



    Resolution chain:

      1. OSMWikiDataHierarchy + CountryPipelineProfile.iso2

      2. Non-sovereign synthetic registry (for territories like Wales/Scotland)

      3. get_country_relations_dict() cross-reference



    Returns '' (empty string) if unresolvable, never None.

    """

    if not input_code:

        return ''



    code = input_code.strip().upper()



    # Already looks like a real ISO 3166-1 alpha-2 (2 letters) - return as-is

    if len(code) == 2 and code.isalpha():

        return code



    # Check non-sovereign synthetic registry first (fast, no DB hit)

    try:

        from core.services.planet_init.non_sovereign_territories import is_non_sovereign_synthetic_iso

        if is_non_sovereign_synthetic_iso(code):

            return code

    except Exception:

        pass



    # Try OSMWikiDataHierarchy lookup by QID

    try:

        hierarchy = OSMWikiDataHierarchy.objects.filter(

            wikidata_id__iexact=code,

        ).first()

        if hierarchy:

            from core.models import CountryPipelineProfile

            profile = CountryPipelineProfile.objects.filter(

                osm_relation_id=hierarchy.osm_relation_id,

            ).first()

            if profile and profile.iso2:

                return profile.iso2.upper()

            # Fallback: cross-reference via get_country_relations_dict()

            relations = get_country_relations_dict()

            for iso_k, data in relations.items():

                if (

                    (data.get("wkg_uri") or "").endswith(code)

                    or (data.get("wikidata_id") or "").upper() == code

                ):

                    return iso_k

    except Exception:

        pass



    # Try OSMWikiDataHierarchy lookup by name

    try:

        from core.services.snapshot.regional_path_service import normalize_country_slug



        hierarchy = OSMWikiDataHierarchy.objects.filter(

            name__iexact=code,

        ).first()

        if not hierarchy:

            hierarchy = OSMWikiDataHierarchy.objects.filter(

                name__iexact=code.replace("_", " "),

            ).first()

        if not hierarchy:

            hierarchy = OSMWikiDataHierarchy.objects.filter(

                name__icontains=code,

            ).first()

        if hierarchy:

            from core.models import CountryPipelineProfile

            profile = CountryPipelineProfile.objects.filter(

                osm_relation_id=hierarchy.osm_relation_id,

            ).first()

            if profile and profile.iso2:

                return profile.iso2.upper()

            relations = get_country_relations_dict()

            for iso_k, data in relations.items():

                norm_slug = normalize_country_slug(data.get("name", ""))

                if norm_slug == normalize_country_slug(code):

                    return iso_k

    except Exception:

        pass



    # Try via get_country_by_name as a final fallback

    try:

        country_meta = get_country_by_name(code)

        if country_meta:

            slug = country_meta.get("slug", "")

            from core.models import CountryPipelineProfile

            profile = CountryPipelineProfile.objects.filter(

                canonical_slug__iexact=slug,

            ).first()

            if profile and profile.iso2:

                return profile.iso2.upper()

    except Exception:

        pass



    # Try slug-based fuzzy matching (handles cases where the display name

    # differs from the DB name, e.g. "Ireland And Northern Ireland" → "Ireland")

    try:

        iso_from_name = resolve_iso_from_country_name(code)

        if iso_from_name:

            return iso_from_name.upper()

    except Exception:

        pass



    return ''







def resolve_iso_from_country_name(country_name: str) -> Optional[str]:

    """

    Resolve an ISO code from a country name by matching against the

    country_relations dict (slug / name fuzzy match).



    This consolidates the duplicate ``_resolve_iso_from_country_name``

    helpers that previously lived in ``api/country_search_views.py``.



    Args:

        country_name: Country name (e.g., "Ireland")



    Returns:

        ISO code (e.g., "IE") or None if not found.

    """

    from core.services.snapshot.regional_path_service import normalize_country_slug



    relations = get_country_relations_dict()

    if not relations:

        return None



    search_term = normalize_country_slug(country_name)



    for key, data in relations.items():

        slug = normalize_country_slug(data.get('slug', ''))

        name = normalize_country_slug(data.get('name', ''))



        if slug == search_term or name == search_term or search_term in slug or slug in search_term:

            iso_code = (data.get('iso_code') or key or '').upper()

            return iso_code or None



    return None


