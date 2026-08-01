"""
Management command to run the 4-phase temporal pre-processing pipeline
for a single country.

This wraps ``TemporalOrchestratorService.run_pipeline()`` and is the
standalone pre-processing step needed before ``run_pipeline`` can execute.

Phases:
    1. Extract region PBF from continent PBF via OSM relation ID
    2. Create single snapshot via time-filter
    3. Generate .poly boundary file from snapshot + relation ID
    4. Generate GeoVectors spatial index (pickle)

Usage:
    python manage.py preprocess_country MC --continent europe

    # With custom snapshot date
    python manage.py preprocess_country MC \
        --continent europe --snapshot-date 2025_12_31

    # Skip phases (e.g., only generate poly)
    python manage.py preprocess_country MC \
        --continent europe --phases 3

    # Include GeoVectors pickle generation
    python manage.py preprocess_country MC \
        --continent europe --phases 1,2,3,4
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run the 4-phase temporal pre-processing pipeline for a single country"

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
            "--phases",
            type=str,
            default="1,2,3",
            help="Comma-separated list of phases to run (default: 1,2,3)",
        )
        parser.add_argument(
            "--snapshot-date",
            type=str,
            default=None,
            help="Snapshot date in YYYY_MM_DD format (default: SINGLE_SNAPSHOT_DATE)",
        )
        parser.add_argument(
            "--source-pbf",
            type=str,
            default=None,
            help="Source PBF path (default: PLANET_OSM_FILE_PATH)",
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
        phases_str = options.get("phases", "1,2,3")
        snapshot_date = options.get("snapshot_date")
        source_pbf = options.get("source_pbf") or settings.PLANET_OSM_FILE_PATH
        wait = options.get("wait", False)

        # Parse phases
        try:
            phases = [int(p.strip()) for p in phases_str.split(",")]
        except ValueError:
            raise CommandError(
                f"Invalid phases format: {phases_str}. "
                "Use comma-separated numbers."
            )

        # Auto-resolve continent if not provided
        if not continent:
            continent = self._resolve_continent(country)

        if not continent:
            raise CommandError(
                f"Cannot resolve continent for '{country}'. "
                "Provide --continent explicitly (e.g., --continent europe)."
            )

        single_snapshot_mode = True

        self.stdout.write(
            self.style.SUCCESS(
                f'\n{"=" * 60}\n'
                f"  Temporal Pre-processing Pipeline\n"
                f"  Country:    {country}\n"
                f"  Continent:  {continent}\n"
                f"  Phases:     {phases}\n"
                f"  Snapshot:   {snapshot_date or 'default'}\n"
                f'{"=" * 60}\n'
            )
        )

        from extraction.services.temporal_orchestrator_service import (
            TemporalOrchestratorService,
        )

        orchestrator = TemporalOrchestratorService()
        result = orchestrator.run_pipeline(
            continent=continent,
            country=country,
            source_pbf_path=source_pbf,
            start_year=2025,
            end_year=2025,
            phases=phases,
            single_snapshot_mode=single_snapshot_mode,
        )

        if result.get("success"):
            self.stdout.write(
                self.style.SUCCESS("\n✓ Pre-processing complete!")
            )
            for phase_num, phase_result in result.get("phases", {}).items():
                status = "✓" if phase_result.get("success") else "✗"
                self.stdout.write(
                    f"  {status} Phase {phase_num}: "
                    f"generated={phase_result.get('generated', 0)}, "
                    f"failed={phase_result.get('failed', 0)}"
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

        TODO: remember to include this in docker
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
