"""Management command: prebuild_wikidata_ids

Offline enrichment of Wikidata identifiers and hierarchy links for
CountryPipelineProfile and SubgraphProfile.

This command is designed to run as part of the prebuild / initialization
pipeline. It:

- Backfills missing `CountryPipelineProfile.wikidata_id` and
  `CountryPipelineProfile.wikidata_uri` from:
    * country_relations.json (via country_relations_payload)
    * OSMWikiDataHierarchy rows at admin_level=2.
- Links countries to their OSMWikiDataHierarchy rows via
  `CountryPipelineProfile.osm_wikidata_hierarchy` when possible.
- Backfills missing `SubgraphProfile.wikidata_id`, `wikidata_uri`, and
  `osm_wikidata_hierarchy` / `osm_relation_id` from OSMWikiDataHierarchy
  rows at subgraph admin levels (4/6/etc.), based on slug+admin_level
  matching.

All enrichment is based on **existing** configuration and hierarchy
state. This command does not perform any live SPARQL/Wikidata queries.

Usage examples (from backend/):

    python manage.py prebuild_wikidata_ids --dry-run
    python manage.py prebuild_wikidata_ids --iso CV
    python manage.py prebuild_wikidata_ids \
        --countries-from-csv ../data/country_identifier_gaps.csv \
        --subgraphs-from-csv ../data/subgraph_igea_gaps.csv
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import models


class Command(BaseCommand):
    help = "Prebuild and backfill Wikidata IDs for countries and subgraphs from existing configs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--iso",
            action="append",
            dest="isos",
            default=None,
            help=(
                "Optional ISO2/ISO3 code to limit enrichment to. "
                "Can be specified multiple times."
            ),
        )
        parser.add_argument(
            "--countries-from-csv",
            type=str,
            default="",
            help=(
                "Optional path to country_identifier_gaps.csv to scope country "
                "enrichment to rows with identifier_gaps."
            ),
        )
        parser.add_argument(
            "--subgraphs-from-csv",
            type=str,
            default="",
            help=(
                "Optional path to subgraph_igea_gaps.csv to scope subgraph "
                "enrichment to rows with cluster_2_wikidata/identifier gaps."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log intended changes without writing to the database.",
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        from orchestration.models import CountryPipelineProfile, SubgraphProfile

        isos: Optional[List[str]] = options.get("isos")
        dry_run: bool = bool(options.get("dry_run"))
        countries_csv: str = options.get("countries_from_csv") or ""
        subgraphs_csv: str = options.get("subgraphs_from_csv") or ""

        iso_filter: Optional[Set[str]] = None
        if isos:
            iso_filter = {iso.upper() for iso in isos if iso}

        country_scope = self._load_country_csv_scope(countries_csv)
        subgraph_scope = self._load_subgraph_csv_scope(subgraphs_csv)

        self.stdout.write(self.style.MIGRATE_HEADING("\nPrebuild Wikidata IDs"))
        self.stdout.write(f"  dry_run = {dry_run}")
        if iso_filter:
            self.stdout.write(f"  ISO filter = {sorted(iso_filter)}")
        if country_scope:
            self.stdout.write(
                f"  Countries CSV scope = {len(country_scope)} rows with identifier gaps"
            )
        if subgraph_scope:
            self.stdout.write(
                f"  Subgraphs CSV scope = {len(subgraph_scope)} rows with cluster_2/3 gaps"
            )

        # Enrich countries
        country_stats = self._enrich_countries(
            CountryPipelineProfile,
            iso_filter=iso_filter,
            country_scope=country_scope,
            dry_run=dry_run,
        )

        # Enrich subgraphs
        subgraph_stats = self._enrich_subgraphs(
            CountryPipelineProfile,
            SubgraphProfile,
            iso_filter=iso_filter,
            subgraph_scope=subgraph_scope,
            dry_run=dry_run,
        )

        self.stdout.write(self.style.SUCCESS("\nSummary:"))
        self.stdout.write(
            f"  Countries updated: {country_stats['countries_updated']} "
            f"(wikidata_id), {country_stats['hierarchy_linked']} linked to hierarchy"
        )
        self.stdout.write(
            f"  Subgraphs updated: {subgraph_stats['subgraphs_updated']} "
            f"(wikidata_id), {subgraph_stats['hierarchy_linked']} linked to hierarchy"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDRY RUN ONLY — no DB writes were made."))

    # ------------------------------------------------------------------
    # Helpers: CSV scopes
    # ------------------------------------------------------------------

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str)
        if not p.is_absolute():
            project_root = Path(settings.BASE_DIR).parent
            p = project_root / path_str
        return p

    def _load_country_csv_scope(self, path_str: str) -> Set[str]:
        """Return a set of ISO codes to consider, based on country CSV.

        We expect `country_identifier_gaps.csv` containing columns like
        country_iso2, country_iso3, identifier_gaps, etc. We treat rows with
        non-empty `identifier_gaps` as in-scope.
        """
        scope: Set[str] = set()
        if not path_str:
            return scope

        path = self._resolve_path(path_str)
        if not path.exists():
            self.stdout.write(
                self.style.WARNING(f"Countries CSV not found at {path}; ignoring.")
            )
            return scope

        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                gaps = (row.get("identifier_gaps") or "").strip()
                if not gaps:
                    continue
                iso2 = (row.get("country_iso2") or "").upper()
                iso3 = (row.get("country_iso3") or "").upper()
                if iso2:
                    scope.add(iso2)
                if iso3:
                    scope.add(iso3)
        return scope

    def _load_subgraph_csv_scope(self, path_str: str) -> Set[Tuple[str, str]]:
        """Return a set of (ISO, slug) keys to consider, based on subgraph CSV.

        We expect `subgraph_igea_gaps.csv` with columns:
          country_iso2, country_iso3, subgraph_slug, igea_block_reasons.

        We treat rows whose `igea_block_reasons` mention `cluster_2_wikidata`
        or `cluster_3_identifier` as in-scope.
        """
        scope: Set[Tuple[str, str]] = set()
        if not path_str:
            return scope

        path = self._resolve_path(path_str)
        if not path.exists():
            self.stdout.write(
                self.style.WARNING(f"Subgraph CSV not found at {path}; ignoring.")
            )
            return scope

        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                reasons = (row.get("igea_block_reasons") or "").lower()
                if not reasons:
                    continue
                if "cluster_2_wikidata" not in reasons and "cluster_3_identifier" not in reasons:
                    continue
                iso2 = (row.get("country_iso2") or "").upper()
                iso3 = (row.get("country_iso3") or "").upper()
                slug = (row.get("subgraph_slug") or "").lower()
                iso = iso2 or iso3
                if not iso or not slug:
                    continue
                scope.add((iso, slug))
        return scope

    # ------------------------------------------------------------------
    # Country enrichment
    # ------------------------------------------------------------------

    def _load_country_relations(self) -> Dict[str, dict]:
        project_root = Path(settings.BASE_DIR).parent
        path = project_root / "data" / "country_relations.json"
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # Fallback: use payload snapshots only
            return {}

    def _enrich_countries(
        self,
        CountryPipelineProfile,
        iso_filter: Optional[Set[str]],
        country_scope: Set[str],
        dry_run: bool,
    ) -> Dict[str, int]:
        from extraction.models import OSMWikiDataHierarchy

        stats = {
            "countries_seen": 0,
            "countries_updated": 0,
            "hierarchy_linked": 0,
        }

        qs = CountryPipelineProfile.objects.all()
        if iso_filter:
            qs = qs.filter(
                models.Q(iso2__in=iso_filter) | models.Q(iso3__in=iso_filter)
            )

        # country_relations.json is already snapshotted into
        # CountryPipelineProfile.country_relations_payload, so we don't
        # require re-reading the JSON here.

        for country in qs.iterator():
            stats["countries_seen"] += 1

            iso2 = (country.iso2 or "").upper()
            iso3 = (country.iso3 or "").upper()

            # CSV scope filter (if provided)
            if country_scope and not ({iso2, iso3} & country_scope):
                continue

            original_wikidata_id = country.wikidata_id
            original_wikidata_uri = country.wikidata_uri
            original_hierarchy_id = country.osm_wikidata_hierarchy_id

            # 1) Try from country_relations_payload
            payload = country.country_relations_payload or {}
            wkg_uri = payload.get("wkg_uri") or payload.get("wkgUri")
            payload_qid: Optional[str] = None
            if wkg_uri and isinstance(wkg_uri, str):
                payload_qid = wkg_uri.rstrip("/").split("/")[-1]

            # 2) Try from linked OSMWikiDataHierarchy (admin_level=2)
            hier: Optional[OSMWikiDataHierarchy] = country.osm_wikidata_hierarchy
            if not hier and country.osm_relation_id:
                hier = (
                    OSMWikiDataHierarchy.objects.filter(
                        osm_relation_id=country.osm_relation_id,
                        admin_level=2,
                    )
                    .order_by("name")
                    .first()
                )

            if not hier and country.wikidata_id:
                hier = (
                    OSMWikiDataHierarchy.objects.filter(
                        wikidata_id=country.wikidata_id,
                        admin_level=2,
                    )
                    .order_by("name")
                    .first()
                )

            hier_qid: Optional[str] = None
            hier_uri: Optional[str] = None
            if hier:
                hier_qid = hier.wikidata_id
                hier_uri = hier.wikidata_uri

            # Decide final QID/URI
            final_qid = country.wikidata_id or payload_qid or hier_qid
            final_uri = country.wikidata_uri or wkg_uri or hier_uri
            if final_qid and not final_uri:
                final_uri = f"https://www.wikidata.org/entity/{final_qid}"

            # Link hierarchy if we found a row and none is linked yet
            link_hierarchy = hier and not original_hierarchy_id

            # Determine if anything will change
            will_update = False
            if final_qid and final_qid != original_wikidata_id:
                will_update = True
            if final_uri and final_uri != original_wikidata_uri:
                will_update = True
            if link_hierarchy:
                will_update = True

            if not will_update:
                continue

            stats["countries_updated"] += 1
            iso_display = iso3 or iso2 or "?"
            self.stdout.write(
                self.style.SUCCESS(
                    f"[country] {country.canonical_name} ({iso_display}) "
                    f"wikidata_id: {original_wikidata_id!r} -> {final_qid!r}"
                )
            )

            if dry_run:
                if link_hierarchy:
                    stats["hierarchy_linked"] += 1
                continue

            # Apply updates
            changed_fields: List[str] = []
            if final_qid and final_qid != original_wikidata_id:
                country.wikidata_id = final_qid
                changed_fields.append("wikidata_id")
            if final_uri and final_uri != original_wikidata_uri:
                country.wikidata_uri = final_uri
                changed_fields.append("wikidata_uri")
            if link_hierarchy and hier:
                country.osm_wikidata_hierarchy = hier
                stats["hierarchy_linked"] += 1
                changed_fields.append("osm_wikidata_hierarchy")

            if changed_fields:
                changed_fields.append("updated_at")
                country.save(update_fields=list(set(changed_fields)))

        return stats

    # ------------------------------------------------------------------
    # Subgraph enrichment
    # ------------------------------------------------------------------

    def _enrich_subgraphs(
        self,
        CountryPipelineProfile,
        SubgraphProfile,
        iso_filter: Optional[Set[str]],
        subgraph_scope: Set[Tuple[str, str]],
        dry_run: bool,
    ) -> Dict[str, int]:
        from extraction.models import OSMWikiDataHierarchy

        stats = {
            "subgraphs_seen": 0,
            "subgraphs_updated": 0,
            "hierarchy_linked": 0,
        }

        # Determine which countries to include
        country_qs = CountryPipelineProfile.objects.all()
        if iso_filter:
            country_qs = country_qs.filter(
                models.Q(iso2__in=iso_filter) | models.Q(iso3__in=iso_filter)
            )

        country_ids = list(country_qs.values_list("id", flat=True))
        if not country_ids:
            return stats

        sg_qs = SubgraphProfile.objects.filter(country_profile_id__in=country_ids)

        for sg in sg_qs.iterator():
            stats["subgraphs_seen"] += 1

            country = sg.country_profile
            iso2 = (country.iso2 or "").upper()
            iso3 = (country.iso3 or "").upper()
            iso = iso2 or iso3
            slug = (sg.slug or "").lower()

            # CSV scope filter (if provided)
            if subgraph_scope and (iso, slug) not in subgraph_scope:
                continue

            original_qid = sg.wikidata_id
            original_uri = sg.wikidata_uri
            original_hierarchy = sg.osm_wikidata_hierarchy

            # 1) Prefer existing hierarchy link
            hier: Optional[OSMWikiDataHierarchy] = original_hierarchy

            # 2) If missing, try slug+admin_level match
            if not hier and sg.admin_level is not None and slug:
                hier = (
                    OSMWikiDataHierarchy.objects.filter(
                        slug=slug,
                        admin_level=sg.admin_level,
                    )
                    .order_by("name")
                    .first()
                )

            # 3) As a weaker fallback, try name+admin_level under same continent
            if not hier and sg.admin_level is not None:
                name = (sg.name or "").strip()
                continent = country.continent_name or sg.continent_name
                if name:
                    qs = OSMWikiDataHierarchy.objects.filter(
                        name=name,
                        admin_level=sg.admin_level,
                    )
                    if continent:
                        qs = qs.filter(continent_name=continent)
                    hier = qs.order_by("name").first()

            hier_qid: Optional[str] = None
            hier_uri: Optional[str] = None
            hier_relation: Optional[int] = None
            if hier:
                hier_qid = hier.wikidata_id
                hier_uri = hier.wikidata_uri
                hier_relation = hier.osm_relation_id

            final_qid = sg.wikidata_id or hier_qid
            final_uri = sg.wikidata_uri or hier_uri
            if final_qid and not final_uri:
                final_uri = f"https://www.wikidata.org/entity/{final_qid}"

            link_hierarchy = hier is not None and original_hierarchy is None

            # Also consider backfilling osm_relation_id from hierarchy
            final_relation = sg.osm_relation_id or hier_relation

            will_update = False
            if final_qid and final_qid != original_qid:
                will_update = True
            if final_uri and final_uri != original_uri:
                will_update = True
            if link_hierarchy:
                will_update = True
            if final_relation and final_relation != sg.osm_relation_id:
                will_update = True

            if not will_update:
                continue

            stats["subgraphs_updated"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[subgraph] {country.canonical_name} / {sg.name} "
                    f"wikidata_id: {original_qid!r} -> {final_qid!r}"
                )
            )

            if dry_run:
                if link_hierarchy:
                    stats["hierarchy_linked"] += 1
                continue

            changed_fields: List[str] = []
            if final_qid and final_qid != original_qid:
                sg.wikidata_id = final_qid
                changed_fields.append("wikidata_id")
            if final_uri and final_uri != original_uri:
                sg.wikidata_uri = final_uri
                changed_fields.append("wikidata_uri")
            if link_hierarchy and hier:
                sg.osm_wikidata_hierarchy = hier
                stats["hierarchy_linked"] += 1
                changed_fields.append("osm_wikidata_hierarchy")
            if final_relation and final_relation != sg.osm_relation_id:
                sg.osm_relation_id = final_relation
                changed_fields.append("osm_relation_id")

            if changed_fields:
                changed_fields.append("updated_at")
                sg.save(update_fields=list(set(changed_fields)))

        return stats
