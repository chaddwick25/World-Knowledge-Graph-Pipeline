"""Management command: prebuild_filesystem_subgraphs

Materialise filesystem-discovered subgraphs into SubgraphProfile rows.

This command complements `prebuild_subgraphs` (which uses the Geofabrik
index) by scanning the actual subgraph directories under
OSM_WIKIDATA_EXTRACTIONS_DIR via the shared `build_subgraph_list` service
and ensuring that every on-disk subgraph has a corresponding
SubgraphProfile.

No external APIs are called; this is purely filesystem + DB metadata.

Typical usage (from backend/):

    python manage.py prebuild_filesystem_subgraphs --dry-run
    python manage.py prebuild_filesystem_subgraphs --iso CV
    python manage.py prebuild_filesystem_subgraphs --iso BZ --iso CA
"""

from __future__ import annotations

from typing import List, Optional, Set

from django.core.management.base import BaseCommand
from django.db import models


class Command(BaseCommand):
    help = (
        "Create or update SubgraphProfile rows for subgraphs discovered "
        "on disk under OSM_WIKIDATA_EXTRACTIONS_DIR."
    )

    def add_arguments(self, parser) -> None:
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
            "--country-name",
            dest="country_name",
            default="",
            help="Optional country canonical_name filter (case-insensitive).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log intended changes without writing to the database.",
        )

    def handle(self, *args, **options) -> None:
        from orchestration.models import CountryPipelineProfile, SubgraphProfile
        from extraction.services.subgraph_list_service import build_subgraph_list

        isos: Optional[List[str]] = options.get("isos")
        country_name: str = (options.get("country_name") or "").strip()
        dry_run: bool = bool(options.get("dry_run"))

        iso_filter: Optional[Set[str]] = None
        if isos:
            iso_filter = {iso.upper() for iso in isos if iso}

        self.stdout.write(self.style.MIGRATE_HEADING("\nPrebuild filesystem subgraphs"))
        self.stdout.write(f"  dry_run = {dry_run}")
        if iso_filter:
            self.stdout.write(f"  ISO filter = {sorted(iso_filter)}")
        if country_name:
            self.stdout.write(f"  Country filter = {country_name!r}")

        qs = CountryPipelineProfile.objects.all()
        if iso_filter:
            qs = qs.filter(
                models.Q(iso2__in=iso_filter) | models.Q(iso3__in=iso_filter)
            )
        if country_name:
            qs = qs.filter(canonical_name__iexact=country_name)

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No countries matched filters; nothing to do."))
            return

        stats = {
            "countries_seen": 0,
            "countries_with_fs_subgraphs": 0,
            "subgraphs_created": 0,
            "subgraphs_updated": 0,
        }

        for country in qs.iterator():
            stats["countries_seen"] += 1

            cname = country.canonical_name
            try:
                fs_subgraphs = build_subgraph_list(country_name=cname, auto_all=True)
            except Exception as exc:  # pragma: no cover - defensive
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! Failed to build subgraph list for {cname}: {exc}"
                    )
                )
                continue

            if not fs_subgraphs:
                continue

            stats["countries_with_fs_subgraphs"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {cname}: discovered {len(fs_subgraphs)} filesystem subgraphs"
                )
            )

            for sg in fs_subgraphs:
                slug_raw = sg.get("slug") or ""
                slug = slug_raw.lower()
                name = sg.get("name") or slug.replace("_", " ") or slug_raw
                poly_path = sg.get("poly_path")

                existing = (
                    SubgraphProfile.objects.filter(
                        country_profile=country,
                        slug=slug,
                    )
                    .order_by("name")
                    .first()
                )

                if existing:
                    changed_fields: List[str] = []

                    if poly_path and existing.subgraph_poly_path != poly_path:
                        if dry_run:
                            self.stdout.write(
                                f"    [update] {cname} / {existing.name}: "
                                f"subgraph_poly_path -> {poly_path}"
                            )
                        existing.subgraph_poly_path = poly_path
                        existing.has_subgraph_poly = True
                        changed_fields.extend([
                            "subgraph_poly_path",
                            "has_subgraph_poly",
                        ])

                    if not existing.continent_name and country.continent_name:
                        existing.continent_name = country.continent_name
                        changed_fields.append("continent_name")

                    if not changed_fields:
                        continue

                    stats["subgraphs_updated"] += 1

                    if dry_run:
                        continue

                    changed_fields.append("updated_at")
                    existing.save(update_fields=list(set(changed_fields)))
                    continue

                # No existing SubgraphProfile — create one
                stats["subgraphs_created"] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    [create] {cname} / {name} (slug={slug}) "
                        f"poly={bool(poly_path)}"
                    )
                )

                if dry_run:
                    continue

                from orchestration.models import SubgraphProfile as SGModel

                SGModel.objects.create(
                    country_profile=country,
                    name=name,
                    slug=slug,
                    continent_name=country.continent_name,
                    subgraph_poly_path=poly_path if poly_path else None,
                    has_subgraph_poly=bool(poly_path),
                    has_subgraph_pbf=False,
                    has_subgraph_pickle=False,
                    metadata_status=SGModel.MetadataStatus.MISSING_RELATION,
                    node_count=0,
                )

        self.stdout.write(self.style.SUCCESS("\nSummary:"))
        self.stdout.write(
            f"  Countries scanned: {stats['countries_seen']} "
            f"({stats['countries_with_fs_subgraphs']} with filesystem subgraphs)"
        )
        self.stdout.write(
            f"  Subgraphs created: {stats['subgraphs_created']}\n"
            f"  Subgraphs updated: {stats['subgraphs_updated']}"
        )
