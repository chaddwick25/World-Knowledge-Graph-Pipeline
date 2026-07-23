import json
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand

from extraction.services.osm_wikidata_resolver import get_country_relations_dict
from extraction.services.regional_path_service import normalize_country_slug
from extraction.services.country_override_service import get_country_slug

def _generate_subgraph_pbf_worker(
    continent: str,
    country: str,
    iso: str,
    snapshot_date: str,
    overwrite: bool,
    relation_id: int,
    wikidata_uri: str,
    country_name: str,
    max_subgraphs: Optional[int],
) -> dict:
    """
    Module-level worker function for multiprocessing.Pool.
    Generates a single subgraph PBF and poly.

    Must be module-level for pickle serialization.
    """
    import django
    django.setup()

    from extraction.services.subgraph_pbf_service import subgraph_pbf_service
    import logging

    logger = logging.getLogger(__name__)

    try:
        res = subgraph_pbf_service.generate_subgraph_pbfs(
            continent=continent,
            country=country,
            iso=iso,
            snapshot_date=snapshot_date,
            max_subgraphs=max_subgraphs,
            overwrite=overwrite,
            relation_id=relation_id,
            wikidata_uri=wikidata_uri,
            country_name=country_name,
        )
        return res
    except Exception as e:
        logger.error(f"Worker failed for {country}/{iso}: {e}", exc_info=True)
        return {
            "success": False,
            "country": country,
            "iso": iso,
            "error": str(e),
        }


class Command(BaseCommand):
    help = (
        "Generate subgraph PBF files and poly files for countries using Wikidata hierarchy. "
        "Uses the country's single snapshot PBF as source and extracts per-subgraph PBFs "
        "based on relation IDs from the hierarchy cache."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--continent",
            type=str,
            help="Optional continent (e.g., europe, africa). Auto-resolved if not provided.",
        )
        parser.add_argument(
            "--countries",
            type=str,
            help="Comma-separated list of country names (e.g., Ireland,France).",
        )
        parser.add_argument(
            "--isos",
            type=str,
            help="Comma-separated list of ISO codes (e.g., IE,FR).",
        )
        parser.add_argument(
            "--max-subgraphs",
            type=int,
            default=None,
            help="Maximum number of subgraphs to process per country (None = all).",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=4,
            help="Number of parallel worker processes (default: 4).",
        )
        parser.add_argument(
            "--snapshot-date",
            type=str,
            default=None,
            help="Snapshot date string (e.g., 2025_12_31). Defaults to SINGLE_SNAPSHOT_DATE.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Regenerate existing PBF and poly files instead of skipping them.",
        )

    def handle(self, *args, **options):
        continent = options.get("continent")
        countries_str = options.get("countries")
        isos_str = options.get("isos")
        max_subgraphs = options.get("max_subgraphs")
        max_workers = options.get("workers", 4)
        snapshot_date = options.get("snapshot_date")
        overwrite = options.get("overwrite", False)

        # Load country relations from OSMWikiDataHierarchy instead of JSON
        country_relations = get_country_relations_dict()
        
        if not country_relations:
            self.stdout.write(
                self.style.ERROR(
                    "No country relations found in OSMWikiDataHierarchy. "
                    "Run 'import_country_relations' first."
                )
            )
            return

        # Resolve target countries/ISOs
        target_entries = []
        if isos_str:
            # Filter by ISO codes (case-insensitive lookup)
            isos = [iso.strip().upper() for iso in isos_str.split(",") if iso.strip()]
            for iso in isos:
                entry = country_relations.get(iso.lower())
                if not entry:
                    # Try case-insensitive search
                    for k, v in country_relations.items():
                        if k.upper() == iso:
                            entry = v
                            break
                if entry:
                    target_entries.append((iso, entry))
                else:
                    self.stdout.write(
                        self.style.WARNING(f"ISO {iso} not found in OSMWikiDataHierarchy")
                    )
        elif countries_str:
            # Filter by country names
            countries = [c.strip() for c in countries_str.split(",") if c.strip()]
            search_terms = [normalize_country_slug(c) for c in countries]
            for iso, entry in country_relations.items():
                slug = normalize_country_slug(entry.get("slug", ""))
                name = normalize_country_slug(entry.get("name", ""))
                if any(st in slug or st in name or slug in st or name in st for st in search_terms):
                    target_entries.append((iso.upper(), entry))
        else:
            self.stdout.write(
                self.style.ERROR("Either --countries or --isos must be provided.")
            )
            return

        if not target_entries:
            self.stdout.write(self.style.WARNING("No matching countries found."))
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Generating subgraph PBFs for {len(target_entries)} countries with {max_workers} workers..."
            )
        )

        # Prepare tasks for multiprocessing
        tasks = []
        for iso, entry in target_entries:
            country_name = entry.get("name")

            # Use JSON-based ISO overrides where available, otherwise fall back to slug
            default_slug = entry.get("slug", normalize_country_slug(country_name))
            country_slug = get_country_slug(iso, default_slug)

            entry_continent = entry.get("continent_name") or entry.get("parent_slug")

            if not entry_continent:
                self.stdout.write(
                    self.style.WARNING(f"Skipping {iso}: missing continent in country_relations")
                )
                continue

            tasks.append({
                "iso": iso,
                "continent": entry_continent,
                "country": country_slug,
                "relation_id": entry.get("relation_id"),
                "wikidata_uri": entry.get("wkg_uri"),
                "country_name": country_name,
            })

        # Close connections in PARENT before fork
        connections.close_all()

        # Process in parallel
        start_time = time.time()

        with Pool(processes=max_workers) as pool:
            results = pool.starmap(
                _generate_subgraph_pbf_worker,
                [
                    (
                        t["continent"],
                        t["country"],
                        t["iso"],
                        snapshot_date,
                        overwrite,
                        t["relation_id"],
                        t["wikidata_uri"],
                        t["country_name"],
                        max_subgraphs,
                    )
                    for t in tasks
                ],
            )

        duration = time.time() - start_time

        # Analyze results
        total_subgraphs = 0
        total_generated = 0
        total_failed = 0
        total_skipped = 0

        for task, res in zip(tasks, results):
            iso = task["iso"]
            country = task["country"]
            if res.get("success"):
                total_subgraphs += res.get("total", 0)
                total_generated += res.get("generated", 0)
                total_failed += res.get("failed", 0)
                total_skipped += res.get("skipped", 0)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [OK] {iso}/{country}: {res.get('generated', 0)} generated, "
                        f"{res.get('skipped', 0)} skipped, {res.get('failed', 0)} failed"
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"  [FAIL] {iso}/{country}: {res.get('error')}")
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Subgraph PBF generation complete in {duration:.1f}s: "
                f"{total_generated} generated, {total_skipped} skipped, {total_failed} failed "
                f"(total subgraphs: {total_subgraphs})"
            )
        )
