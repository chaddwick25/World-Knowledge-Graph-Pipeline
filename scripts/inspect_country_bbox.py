#!/usr/bin/env python
"""Inspect how resolve_country_bbox derives a country's bounding box.

For each ISO country code provided, this script prints:

- The final bbox from extraction.services.osm_wikidata_resolver.resolve_country_bbox
- Any OsmBoundary records for that ISO code (and their stored bbox)
- Any PolygonFile records whose region_name looks related to the country
  (by ISO code or a simple name hint), plus bbox parsed from their .poly file
- Any CountryPipelineProfile records for that ISO code and any stored bbox
  in country_relations_payload

Usage (from repo root):

    cd backend
    python ../scripts/inspect_country_bbox.py --country MC --country MZ

If no --country codes are provided, the script defaults to ["MC", "MZ"].
"""

import argparse
import os
import pathlib
import sys
from typing import List

import django


def setup_django() -> None:
    """Configure Django so we can use ORM/models from a standalone script."""

    # Repo root: scripts/ is a sibling of backend/
    root = pathlib.Path(__file__).resolve().parents[1]
    backend_dir = root / "backend"

    # Ensure backend package is importable as "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    django.setup()


def iso_to_name_hint(code: str) -> str:
    """Best-effort mapping from ISO alpha-2 code to a PolygonFile name hint.

    This mirrors the helper used in worldkg_nca.services.wikidata_service.
    """

    hints = {
        "DE": "germany",       "GB": "great-britain",  "FR": "france",
        "IT": "italy",         "US": "united-states",  "CA": "canada",
        "AU": "australia",     "NL": "netherlands",    "BE": "belgium",
        "AT": "austria",       "CH": "switzerland",    "ES": "spain",
        "PL": "poland",        "SE": "sweden",         "NO": "norway",
        "JP": "japan",         "CN": "china",          "BR": "brazil",
        "ZA": "south-africa",  "NG": "nigeria",        "TZ": "tanzania",
        "JM": "jamaica",       "CR": "costa-rica",     "GT": "guatemala",
        "HN": "honduras",      "SV": "el-salvador",    "NI": "nicaragua",
        "PA": "panama",        "BZ": "belize",         "CU": "cuba",
        "HT": "haiti",         "DO": "dominican-republic", "MX": "mexico",
    }
    return hints.get(code.upper(), code.lower())


def inspect_country(code: str) -> None:
    from django.db.models import Q

    from extraction.services import osm_wikidata_resolver
    from extraction.models import OsmBoundary, PolygonFile
    from orchestration.models import CountryPipelineProfile

    iso = code.upper()
    print("=" * 72)
    print(f"Country: {iso}")

    # Final bbox from the canonical resolver
    final_bbox = osm_wikidata_resolver.resolve_country_bbox(iso)
    print(f"resolve_country_bbox({iso!r}) -> {final_bbox}")

    # 1. OsmBoundary records
    obs: List[OsmBoundary] = list(OsmBoundary.objects.filter(iso_code__iexact=iso))
    print(f"OsmBoundary records (iso_code={iso}): count={len(obs)}")
    for ob in obs:
        print(f"  id={ob.id}, iso_code={ob.iso_code}, bbox={getattr(ob, 'bbox', None)}")

    # 2. PolygonFile candidates, with bbox parsed from the .poly file
    hint = iso_to_name_hint(iso)
    pfs: List[PolygonFile] = list(
        PolygonFile.objects.filter(
            Q(region_name__icontains=iso) | Q(region_name__icontains=hint),
            is_active=True,
        )
    )
    print(
        "PolygonFile candidates (region_name contains %r or %r, is_active=True): "
        "count=%d" % (iso, hint, len(pfs))
    )
    for pf in pfs:
        poly_bbox = None
        if pf.file_path and os.path.exists(pf.file_path):
            try:
                poly_bbox = osm_wikidata_resolver.parse_poly_bbox(pf.file_path)
            except Exception as exc:  # pragma: no cover - debug output only
                poly_bbox = f"error parsing poly: {exc!r}"
        print(
            f"  id={pf.id}, region_name={pf.region_name!r}, "
            f"file_path={pf.file_path!r}, bbox_from_poly={poly_bbox}"
        )

    # 3. CountryPipelineProfile records (including any stored bbox in payload)
    profiles: List[CountryPipelineProfile] = list(
        CountryPipelineProfile.objects.filter(
            Q(iso2__iexact=iso) | Q(iso3__iexact=iso)
        )
    )
    print(f"CountryPipelineProfile records (iso2/iso3={iso}): count={len(profiles)}")
    for profile in profiles:
        payload = getattr(profile, "country_relations_payload", {}) or {}
        print(
            "  id=%s, iso2=%s, iso3=%s, canonical_slug=%s, continent_name=%s" % (
                profile.id,
                getattr(profile, "iso2", None),
                getattr(profile, "iso3", None),
                getattr(profile, "canonical_slug", None),
                getattr(profile, "continent_name", None),
            )
        )
        print(f"    snapshot_pbf_path={getattr(profile, 'snapshot_pbf_path', None)!r}")
        print(f"    snapshot_poly_path={getattr(profile, 'snapshot_poly_path', None)!r}")
        print(f"    payload.bbox={payload.get('bbox')!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect country bbox resolution.")
    parser.add_argument(
        "--country",
        "-c",
        action="append",
        dest="countries",
        help="ISO 3166-1 alpha-2/alpha-3 country code (repeatable)",
    )
    args = parser.parse_args()

    countries = args.countries or ["MC", "MZ"]

    setup_django()

    for code in countries:
        inspect_country(code)


if __name__ == "__main__":
    main()
