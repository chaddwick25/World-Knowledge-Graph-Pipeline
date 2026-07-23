"""
Management command: enrich_with_places

Best-effort Google Places enrichment for OSM entities lacking Wikidata alignment.

Usage:
    poetry run python manage.py enrich_with_places --iso CV
    poetry run python manage.py enrich_with_places --iso CV --max-entities 500
    poetry run python manage.py enrich_with_places --iso CV --dry-run
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Enrich OSM entities with Google Places data (best-effort, non-blocking)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--iso",
            type=str,
            required=True,
            help="ISO 3166-1 alpha-2 country code (e.g., CV, MZ)",
        )
        parser.add_argument(
            "--max-entities",
            type=int,
            default=1000,
            help="Maximum entities to process (default: 1000)",
        )
        parser.add_argument(
            "--min-uslp-score",
            type=float,
            default=0.5,
            help="Minimum USLP score below which to attempt enrichment (default: 0.5)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count eligible entities without making API calls",
        )

    def handle(self, *args, **options):
        iso = options["iso"].upper()
        max_entities = options["max_entities"]
        min_uslp_score = options["min_uslp_score"]
        dry_run = options["dry_run"]

        from extraction.services.places_enrichment_service import (
            get_places_enrichment_service,
        )

        svc = get_places_enrichment_service()

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run: would enrich up to {max_entities} entities for {iso}"
                )
            )
            self.stdout.write(
                f"  API key configured: {bool(svc.api_key)}"
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Enriching {iso} with Google Places data "
                f"(max_entities={max_entities}, min_uslp_score={min_uslp_score})"
            )
        )

        result = svc.enrich_country(
            iso=iso,
            max_entities=max_entities,
            min_uslp_score=min_uslp_score,
        )

        self.stdout.write(self.style.SUCCESS(f"Complete: {result}"))
