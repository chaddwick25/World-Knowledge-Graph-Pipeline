from django.core.management.base import BaseCommand
from django.conf import settings
from pathlib import Path
import json

from extraction.services.osm_relation_hierarchy_service import (
    osm_relation_hierarchy_service,
    HierarchyConfig,
)


class Command(BaseCommand):
    help = "Builds OSM relation-based admin hierarchies for selected countries and injects them into data/country_relations.json. (LEGACY - Use import_country_relations for OSMWikiDataHierarchy)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--countries",
            type=str,
            default=None,
            help="Comma-separated list of ISO country codes to process (default: all in country_relations.json)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force refresh of hierarchy cache for the selected countries",
        )

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        json_path = base_dir / "data" / "country_relations.json"

        if not json_path.exists():
            self.stdout.write(self.style.ERROR(f"country_relations.json not found at {json_path}. Run 'sync_country_relations' first."))
            return

        with open(json_path, "r") as f:
            data = json.load(f)

        countries_opt = options.get("countries")
        if countries_opt:
            target_isos = {c.strip().upper() for c in countries_opt.split(",") if c.strip()}
        else:
            target_isos = set(data.keys())

        # Config-driven: we could later expose max_depth/include_admin_levels via settings or flags
        hierarchy_config = HierarchyConfig()

        updated = 0

        for iso in sorted(target_isos):
            entry = data.get(iso)
            if not entry:
                self.stdout.write(self.style.WARNING(f"ISO {iso} not found in country_relations.json, skipping."))
                continue

            root_rel = entry.get("relation_id")
            if not root_rel:
                self.stdout.write(self.style.WARNING(f"ISO {iso} has no relation_id, skipping."))
                continue

            self.stdout.write(f"Building hierarchy for {iso} (relation {root_rel})...")

            # For now we ignore --force and rely on the service to manage caching.
            # In a later iteration we can pass it through the config.
            hierarchy = osm_relation_hierarchy_service.build_hierarchy_for_country(
                iso,
                root_rel,
                wikidata_uri=entry.get("wkg_uri"),
                country_name=entry.get("name"),
            )
            entry["hierarchy"] = hierarchy
            updated += 1

        with open(json_path, "w") as f:
            json.dump(data, f, indent=4)

        self.stdout.write(self.style.SUCCESS(f"Updated hierarchies for {updated} countries in {json_path}"))
