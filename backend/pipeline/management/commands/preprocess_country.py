"""
Management command to run the snapshot pre-processing pipeline
for a single country.

This wraps ``SnapshotExtractionService.extract_country_snapshot()`` and is the
standalone pre-processing step needed before ``run_pipeline`` can execute.

Flow (post-refactor — see docs/plans/TEMPORAL_SNAPSHOT_REFACTOR.md):
    1. Resolve the .poly file (caller-supplied → OsmBoundary → generate).
    2. ``osmium extract --with-referenced --polygon`` from the planet PBF.
    3. ``osmium time-filter`` to flatten history to the snapshot date.
    4. Register a PbfFile row for the snapshot.

Usage:
    python manage.py preprocess_country MC --continent europe

    # With custom snapshot date
    python manage.py preprocess_country MC \
        --continent europe --snapshot-date 2025_12_31
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run the snapshot pre-processing pipeline for a single country"

    def add_arguments(self, parser):
        parser.add_argument(
            "country",
            type=str,
            help="Country name or ISO code (e.g., 'Monaco', 'MC')",
        )
        parser.add_argument(
            "--continent",
            type=str,
            default=None,
            help="Continent slug (e.g., 'europe'). Auto-resolved if omitted.",
        )
        parser.add_argument(
            "--snapshot-date",
            type=str,
            default=None,
            help="Snapshot date in YYYY_MM_DD format (default: SINGLE_SNAPSHOT_DATE)",
        )
        parser.add_argument(
            "--wait",
            action="store_true",
            default=False,
            help="Wait for completion and print results",
        )

    def handle(self, *args, **options):
        country = options["country"]
        continent = options.get("continent")
        snapshot_date = options.get("snapshot_date") or getattr(
            settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31"
        )
        wait = options.get("wait", False)

        # Auto-resolve continent if not provided
        if not continent:
            continent = self._resolve_continent(country)

        if not continent:
            raise CommandError(
                f"Cannot resolve continent for '{country}'. "
                "Provide --continent explicitly (e.g., --continent europe)."
            )

        # Resolve ISO + OSM relation ID for the country
        iso = self._resolve_iso(country)
        if not iso:
            raise CommandError(
                f"Cannot resolve ISO code for '{country}'. "
                "Ensure the country exists in CountryPipelineProfile or EligibleCountry."
            )

        osm_relation_id = self._resolve_osm_relation_id(iso)

        self.stdout.write(
            self.style.SUCCESS(
                f'\n{"=" * 60}\n'
                f"  Snapshot Pre-processing\n"
                f"  Country:    {country} (ISO={iso})\n"
                f"  Continent:  {continent}\n"
                f"  Snapshot:   {snapshot_date}\n"
                f"  Relation:   {osm_relation_id or 'N/A'}\n"
                f'{"=" * 60}\n'
            )
        )

        from extraction.services.snapshot_extraction_service import (
            SnapshotExtractionService,
        )

        service = SnapshotExtractionService()
        result = service.extract_country_snapshot(
            country_code=iso,
            country_name=country,
            continent=continent,
            snapshot_date=snapshot_date,
            osm_relation_id=osm_relation_id,
        )

        if result.get("success"):
            self.stdout.write(
                self.style.SUCCESS("\n✓ Pre-processing complete!")
            )
            self.stdout.write(
                f"  Snapshot PBF: {result.get('snapshot_pbf_path')}\n"
                f"  Poly file:    {result.get('poly_file_path')}\n"
                f"  Entities:     {result.get('entity_count')}\n"
                f"  Skipped:      {result.get('skipped', False)}"
            )
        else:
            error = result.get("error", "Unknown error")
            self.stdout.write(
                self.style.ERROR(f"\n✗ Pre-processing failed: {error}")
            )
            raise CommandError(error)

        if wait:
            self.stdout.write("\nResults:")
            self.stdout.write(json.dumps(result, indent=2, default=str))

    def _resolve_iso(self, country: str) -> str:
        """Resolve ISO 3166-1 alpha-2 code from CountryPipelineProfile."""
        try:
            from orchestration.models import CountryPipelineProfile, EligibleCountry
            profile = CountryPipelineProfile.objects.filter(
                iso2__iexact=country,
            ).first()
            if not profile:
                profile = CountryPipelineProfile.objects.filter(
                    canonical_name__iexact=country,
                ).first()
            if profile:
                return profile.iso2
            eligible = EligibleCountry.objects.filter(
                iso_code__iexact=country,
            ).first()
            if eligible:
                return eligible.iso_code
        except Exception:
            pass
        return None

    def _resolve_osm_relation_id(self, iso: str):
        """Resolve OSM relation ID from CountryPipelineProfile."""
        try:
            from orchestration.models import CountryPipelineProfile
            profile = CountryPipelineProfile.objects.filter(
                iso2__iexact=iso,
            ).first()
            if profile and profile.osm_relation_id:
                return profile.osm_relation_id
        except Exception:
            pass
        return None

    def _resolve_continent(self, country: str) -> str:
        """Resolve continent name from OSMWikiDataHierarchy or country_relations.json."""
        # Try DB first
        try:
            from extraction.models import OSMWikiDataHierarchy

            # Try ISO code first
            entry = OSMWikiDataHierarchy.objects.filter(
                slug__iexact=country.lower(),
            ).exclude(parent_slug__isnull=True).first()

            if not entry:
                entry = OSMWikiDataHierarchy.objects.filter(
                    name__iexact=country,
                ).exclude(parent_slug__isnull=True).first()

            if entry and entry.parent_slug:
                self.stdout.write(
                    f"  Resolved continent: {entry.parent_slug} (from DB)"
                )
                return entry.parent_slug
        except Exception:
            pass

        # TODO: remember to include this in docker
        # Fallback to country_relations.json
        rel_path = (
            Path(settings.BASE_DIR).parent / "data" / "country_relations.json"
        )
        if rel_path.exists():
            try:
                with open(rel_path, "r") as f:
                    relations = json.load(f)
                iso = country.upper()
                if iso in relations:
                    parent = relations[iso].get("parent_slug")
                    if parent:
                        self.stdout.write(
                            f"  Resolved continent: {parent} "
                            "(from country_relations.json)"
                        )
                        return parent
                # Try name match
                for code, data in relations.items():
                    if data.get("name", "").lower() == country.lower():
                        parent = data.get("parent_slug")
                        if parent:
                            self.stdout.write(
                                f"  Resolved continent: {parent} "
                                "(from country_relations.json)"
                            )
                            return parent
            except Exception:
                pass

        return None
