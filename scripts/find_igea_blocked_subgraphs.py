#!/usr/bin/env python
"""Find sub-divisions (SubgraphProfile) that cannot realistically run IGEA.

This script uses the same identifier-resolution assumptions described in
`docs/contracts/IDENTIFIER_RESOLUTION.md`, extended to subgraphs:

- A subgraph should be linked to an OSM/Wikidata hierarchy row.
- It should have a valid OSM relation and Wikidata ID.
- It should have polygon / PBF artifacts and a subgraph pickle.

If any of these are missing, the subgraph is considered "blocked" for IGEA
(or only partially supported). The script prints a per-country summary and
optionally writes a CSV with one row per subgraph.

Usage (from project root):

    poetry run python scripts/find_igea_blocked_subgraphs.py \
        --country-iso CV \
        --output data/subgraph_igea_gaps.csv

"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

# ---------------------------------------------------------------------------
# Django setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

import django  # type: ignore  # noqa: E402


def django_setup() -> None:
    django.setup()  # type: ignore


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def compute_igea_block_reasons(subgraph) -> List[str]:
    """Return a list of *cluster* labels why this subgraph cannot run IGEA.

    Instead of low-level codes, we map configuration gaps into three clusters
    (from the Places & Open Data augmentation plan):

        - cluster_1_boundary   → boundary / PBF / poly / extract issues
        - cluster_2_wikidata   → Wikidata / hierarchy coverage issues
        - cluster_3_identifier → identifier & pipeline contract coherence

    This is configuration-based only (no live IGEA run stats).
    """
    from orchestration.models import SubgraphProfile  # type: ignore

    # Hierarchy / Wikidata anchors
    missing_hierarchy = subgraph.osm_wikidata_hierarchy_id is None
    missing_wikidata = not bool(subgraph.wikidata_id)

    # Explicit OSM relation diagnostics
    missing_osm_rel = not bool(subgraph.osm_relation_id) or (
        subgraph.metadata_status == SubgraphProfile.MetadataStatus.MISSING_RELATION
    )

    # Geometry / files
    missing_poly = not bool(subgraph.has_subgraph_poly)
    missing_pbf = not bool(subgraph.has_subgraph_pbf)

    # Node count == 0 suggests there is no usable OSM extract for this region.
    empty_extract = not subgraph.node_count or subgraph.node_count == 0

    # Pipeline artifacts (pickle is a strong proxy for IGEA readiness)
    missing_pickle = not bool(subgraph.has_subgraph_pickle) or (
        subgraph.metadata_status == SubgraphProfile.MetadataStatus.NO_PICKLE
    )

    orphan_poly = subgraph.metadata_status == SubgraphProfile.MetadataStatus.ORPHAN_POLY

    clusters = set()

    # Cluster 1 — Boundary / Bounding Box Resolution
    if missing_poly or missing_pbf or empty_extract or orphan_poly:
        clusters.add("cluster_1_boundary")

    # Cluster 2 — Wikidata Geometric Alignment / Link Enrichment
    if missing_hierarchy or missing_wikidata:
        clusters.add("cluster_2_wikidata")

    # Cluster 3 — Identifier & Pipeline Contract Coherence
    if missing_osm_rel or missing_pickle:
        clusters.add("cluster_3_identifier")

    return sorted(clusters)


def build_queryset(country_iso: str = "", country_name: str = ""):
    from orchestration.models import SubgraphProfile  # type: ignore

    qs = SubgraphProfile.objects.select_related("country_profile").all()

    if country_iso:
        country_iso_upper = country_iso.upper()
        qs = qs.filter(
            country_profile__iso2__iexact=country_iso_upper
        ) | qs.filter(
            country_profile__iso3__iexact=country_iso_upper
        )

    if country_name:
        qs = qs.filter(country_profile__canonical_name__iexact=country_name)

    return qs.order_by("country_profile__canonical_name", "name")


def run(country_iso: str = "", country_name: str = "", output: str = "") -> None:
    django_setup()

    rows: List[Dict[str, object]] = []
    existing_keys: set = set()

    qs = build_queryset(country_iso=country_iso, country_name=country_name)

    # Start with DB-backed SubgraphProfile rows (authoritative when present)
    for sg in qs.iterator():
        country = sg.country_profile
        reasons = compute_igea_block_reasons(sg)
        blocked = bool(reasons)

        country_key = (country.iso2 or country.iso3 or country.canonical_name).upper()
        slug_key = (sg.slug or "").lower()
        existing_keys.add((country_key, slug_key))

        row = {
            "country_iso2": country.iso2 or "",
            "country_iso3": country.iso3 or "",
            "country_name": country.canonical_name,
            "subgraph_name": sg.name,
            "subgraph_slug": sg.slug,
            "admin_level": sg.admin_level,
            "metadata_status": sg.metadata_status,
            "has_subgraph_poly": sg.has_subgraph_poly,
            "has_subgraph_pbf": sg.has_subgraph_pbf,
            "has_subgraph_pickle": sg.has_subgraph_pickle,
            "node_count": sg.node_count or 0,
            "osm_relation_id": sg.osm_relation_id,
            "wikidata_id": sg.wikidata_id or "",
            "blocked_for_igea": blocked,
            "igea_block_reasons": ";".join(reasons),
        }
        rows.append(row)

    # Augment with filesystem-discovered subgraphs for all relevant countries,
    # including those that have no SubgraphProfile rows yet.
    rows = augment_with_filesystem_subgraphs(
        rows,
        existing_keys,
        country_iso=country_iso,
        country_name=country_name,
    )

    # Recompute per-country stats from the final row set (DB + filesystem).
    per_country_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"total": 0, "blocked": 0, "ok": 0}
    )

    for row in rows:
        country_label = f"{row['country_name']} ({row['country_iso2'] or row['country_iso3'] or '?'})"
        per_country_stats[country_label]["total"] += 1
        if row["blocked_for_igea"]:
            per_country_stats[country_label]["blocked"] += 1
        else:
            per_country_stats[country_label]["ok"] += 1

    # Print human-readable summary, focused on blocked subgraphs
    print("\n=== IGEA Subgraph Coverage Summary ===")
    for country_label, stats in sorted(per_country_stats.items()):
        total = stats["total"]
        blocked = stats["blocked"]
        ok = stats["ok"]
        print(f"- {country_label}: total={total}, blocked={blocked}, ok={ok}")

    print("\nSubgraphs blocked for IGEA (by configuration):")
    for row in rows:
        if not row["blocked_for_igea"]:
            continue
        print(
            f"  - {row['country_name']} ({row['country_iso2'] or row['country_iso3'] or '?'}) / "
            f"{row['subgraph_name']} [{row['subgraph_slug']}] — reasons: {row['igea_block_reasons']}"
        )

    # Optional per-subgraph CSV export
    if output:
        output_path = Path(output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "country_iso2",
            "country_iso3",
            "country_name",
            "subgraph_name",
            "subgraph_slug",
            "admin_level",
            "metadata_status",
            "has_subgraph_poly",
            "has_subgraph_pbf",
            "has_subgraph_pickle",
            "node_count",
            "osm_relation_id",
            "wikidata_id",
            "blocked_for_igea",
            "igea_block_reasons",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        print(f"\nWrote {len(rows)} subgraph rows to {output_path}")


def augment_with_filesystem_subgraphs(
    rows: List[Dict[str, object]],
    existing_keys: set,
    country_iso: str = "",
    country_name: str = "",
) -> List[Dict[str, object]]:
    """Discover subgraphs from filesystem for all countries and add synthetic rows.

    This uses the same logic as USLP (build_subgraph_list) to inspect
    OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/subgraphs and adds a
    synthetic SubgraphProfile-like row when no DB SubgraphProfile exists.
    """

    from orchestration.models import CountryPipelineProfile  # type: ignore
    from extraction.services.subgraph_list_service import build_subgraph_list  # type: ignore

    # ------------------------------------------------------------------
    # 1) DB-backed countries
    # ------------------------------------------------------------------

    country_qs = CountryPipelineProfile.objects.all()

    if country_iso:
        country_iso_upper = country_iso.upper()
        country_qs = country_qs.filter(iso2__iexact=country_iso_upper) | country_qs.filter(
            iso3__iexact=country_iso_upper
        )

    if country_name:
        country_qs = country_qs.filter(canonical_name__iexact=country_name)

    for country in country_qs.iterator():
        country_key = (country.iso2 or country.iso3 or country.canonical_name).upper()
        try:
            fs_subgraphs = build_subgraph_list(country_name=country.canonical_name, auto_all=True)
        except Exception:
            continue

        for sg in fs_subgraphs:
            slug = (sg.get("slug") or "").lower()
            key = (country_key, slug)
            if key in existing_keys:
                continue

            poly_path = sg.get("poly_path")

            row = {
                "country_iso2": country.iso2 or "",
                "country_iso3": country.iso3 or "",
                "country_name": country.canonical_name,
                "subgraph_name": sg.get("name") or sg.get("slug") or "",
                "subgraph_slug": sg.get("slug") or "",
                "admin_level": None,
                "metadata_status": "MISSING_SUBGRAPH_PROFILE",
                "has_subgraph_poly": bool(poly_path),
                "has_subgraph_pbf": False,
                "has_subgraph_pickle": False,
                "node_count": 0,
                "osm_relation_id": None,
                "wikidata_id": "",
                "blocked_for_igea": True,
                "igea_block_reasons": "missing_subgraph_profile",
            }
            rows.append(row)

    # ------------------------------------------------------------------
    # 2) JSON-only countries from backend/data/country_relations.json
    # ------------------------------------------------------------------

    relations_path = BACKEND_DIR / "data" / "country_relations.json"
    legacy_relations: Dict[str, Dict] = {}
    if relations_path.exists():
        try:
            with open(relations_path, "r", encoding="utf-8") as f:
                legacy_relations = json.load(f)
        except Exception:
            legacy_relations = {}

    if legacy_relations:
        # Build quick lookup of which ISO codes already have CountryPipelineProfile
        seen_iso2: set = set()
        seen_iso3: set = set()
        for c in CountryPipelineProfile.objects.all().only("iso2", "iso3"):
            if c.iso2:
                seen_iso2.add(c.iso2.upper())
            if c.iso3:
                seen_iso3.add(c.iso3.upper())

        iso_filter = (country_iso or "").upper()
        name_filter = (country_name or "").strip().lower()

        for iso_code, payload in legacy_relations.items():
            iso_norm = (iso_code or "").upper()

            # Skip if this ISO is already covered by a CountryPipelineProfile
            if iso_norm in seen_iso2 or iso_norm in seen_iso3:
                continue

            # Apply optional filters
            if iso_filter and iso_norm != iso_filter:
                continue
            if name_filter and (payload.get("name") or "").strip().lower() != name_filter:
                continue

            name = payload.get("name") or iso_norm
            country_key = iso_norm

            try:
                fs_subgraphs = build_subgraph_list(country_name=name, auto_all=True)
            except Exception:
                continue

            iso2_val = iso_norm if len(iso_norm) == 2 else ""
            iso3_val = iso_norm if len(iso_norm) == 3 else ""

            for sg in fs_subgraphs:
                slug = (sg.get("slug") or "").lower()
                key = (country_key, slug)
                if key in existing_keys:
                    continue

                poly_path = sg.get("poly_path")

                row = {
                    "country_iso2": iso2_val,
                    "country_iso3": iso3_val,
                    "country_name": name,
                    "subgraph_name": sg.get("name") or sg.get("slug") or "",
                    "subgraph_slug": sg.get("slug") or "",
                    "admin_level": None,
                    "metadata_status": "MISSING_COUNTRY_PROFILE",
                    "has_subgraph_poly": bool(poly_path),
                    "has_subgraph_pbf": False,
                    "has_subgraph_pickle": False,
                    "node_count": 0,
                    "osm_relation_id": None,
                    "wikidata_id": "",
                    "blocked_for_igea": True,
                    "igea_block_reasons": "missing_country_profile;missing_subgraph_profile",
                }
                rows.append(row)

    return rows


def build_country_identifier_rows(country_iso: str = "", country_name: str = "") -> List[Dict[str, object]]:
    from orchestration.models import CountryPipelineProfile  # type: ignore

    qs = CountryPipelineProfile.objects.all().prefetch_related("subgraphs")

    if country_iso:
        country_iso_upper = country_iso.upper()
        qs = qs.filter(iso2__iexact=country_iso_upper) | qs.filter(iso3__iexact=country_iso_upper)

    if country_name:
        qs = qs.filter(canonical_name__iexact=country_name)

    rows: List[Dict[str, object]] = []

    seen_iso2: set = set()
    seen_iso3: set = set()

    for country in qs.iterator(chunk_size=100):
        iso2 = country.iso2 or ""
        iso3 = country.iso3 or ""
        wikidata_id = country.wikidata_id or ""
        osm_relation_id = country.osm_relation_id

        if iso2:
            seen_iso2.add(iso2.upper())
        if iso3:
            seen_iso3.add(iso3.upper())

        identifier_gaps = []
        if not iso2 and not iso3:
            identifier_gaps.append("missing_iso_code")
        if not wikidata_id:
            identifier_gaps.append("missing_wikidata_id")
        if not osm_relation_id:
            identifier_gaps.append("missing_osm_relation_id")

        missing_id_reasons = {
            "missing_osm_wikidata_hierarchy",
            "missing_wikidata_id",
            "missing_osm_relation",
        }

        nonexistent_subdivisions: List[str] = []
        for sg in country.subgraphs.all():
            reasons = compute_igea_block_reasons(sg)
            if not missing_id_reasons.intersection(reasons):
                continue
            label = sg.name
            if sg.slug:
                label = f"{sg.name} [{sg.slug}]"
            nonexistent_subdivisions.append(label)

        row = {
            "country_iso2": iso2,
            "country_iso3": iso3,
            "country_name": country.canonical_name,
            "country_wikidata_id": wikidata_id,
            "country_osm_relation_id": osm_relation_id,
            "missing_iso_code": not bool(iso2 or iso3),
            "missing_wikidata_id": not bool(wikidata_id),
            "missing_osm_relation_id": not bool(osm_relation_id),
            "identifier_gaps": ";".join(sorted(set(identifier_gaps))) if identifier_gaps else "",
            "nonexistent_subdivisions": ";".join(nonexistent_subdivisions) if nonexistent_subdivisions else "",
        }
        rows.append(row)

    # ------------------------------------------------------------------
    # Include countries that exist only in backend/data/country_relations.json
    # (no CountryPipelineProfile row). These are structural gaps in the
    # identifier-resolution chain: we know about the country from configs
    # but have no DB profile or identifiers wired up for the pipeline.
    # ------------------------------------------------------------------

    relations_path = BACKEND_DIR / "data" / "country_relations.json"
    legacy_relations: Dict[str, Dict] = {}
    if relations_path.exists():
        try:
            with open(relations_path, "r", encoding="utf-8") as f:
                legacy_relations = json.load(f)
        except Exception:
            legacy_relations = {}

    if legacy_relations:
        iso_filter = (country_iso or "").upper()
        name_filter = (country_name or "").strip().lower()

        for iso_code, payload in legacy_relations.items():
            iso_norm = (iso_code or "").upper()

            # Skip if this ISO is already covered by a CountryPipelineProfile
            if iso_norm in seen_iso2 or iso_norm in seen_iso3:
                continue

            # Apply optional filters
            if iso_filter and iso_norm != iso_filter:
                continue
            if name_filter and (payload.get("name") or "").strip().lower() != name_filter:
                continue

            iso2_val = iso_norm if len(iso_norm) == 2 else ""
            iso3_val = iso_norm if len(iso_norm) == 3 else ""

            row = {
                "country_iso2": iso2_val,
                "country_iso3": iso3_val,
                "country_name": payload.get("name") or iso_norm,
                "country_wikidata_id": "",  # legacy JSON does not guarantee QID
                "country_osm_relation_id": None,
                # These config-only countries are missing the DB profile and
                # structured identifiers for the pipeline.
                "missing_iso_code": not bool(iso2_val or iso3_val),
                "missing_wikidata_id": True,
                "missing_osm_relation_id": True,
                "identifier_gaps": "missing_country_profile;missing_wikidata_id;missing_osm_relation_id",
                # No SubgraphProfile rows exist yet, so we cannot list concrete
                # subdivisions here; the entire country is effectively missing.
                "nonexistent_subdivisions": "",
            }
            rows.append(row)

    return rows


def main(argv: List[str] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Find sub-divisions (SubgraphProfile) that are IGEA-blocked."
    )
    parser.add_argument(
        "--country-iso",
        type=str,
        default="",
        help="Optional ISO2/ISO3 country code to filter by (e.g. CV, DE, US).",
    )
    parser.add_argument(
        "--country-name",
        type=str,
        default="",
        help="Optional canonical country name to filter by (e.g. 'Cape Verde').",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Optional per-subgraph CSV output path (relative to project root), e.g. data/subgraph_igea_gaps.csv.",
    )
    parser.add_argument(
        "--country-summary-output",
        type=str,
        default="",
        help=(
            "Optional per-country CSV output path (relative to project root), "
            "e.g. data/country_identifier_gaps.csv."
        ),
    )

    args = parser.parse_args(argv)

    run(
        country_iso=args.country_iso,
        country_name=args.country_name,
        output=args.output,
    )

    if args.country_summary_output:
        django_setup()
        country_rows = build_country_identifier_rows(
            country_iso=args.country_iso,
            country_name=args.country_name,
        )

        output_path = Path(args.country_summary_output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "country_iso2",
            "country_iso3",
            "country_name",
            "country_wikidata_id",
            "country_osm_relation_id",
            "missing_iso_code",
            "missing_wikidata_id",
            "missing_osm_relation_id",
            "identifier_gaps",
            "nonexistent_subdivisions",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in country_rows:
                writer.writerow(row)

        print(f"\nWrote {len(country_rows)} country rows to {output_path}")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
