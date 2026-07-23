import json
from pathlib import Path
from typing import Optional
from multiprocessing import Pool
from functools import partial
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
from extraction.services.osm_wikidata_resolver import get_country_relations_dict
from extraction.services.regional_path_service import normalize_country_slug
from extraction.services.country_override_service import get_country_slug


def _generate_subgraph_pickle_worker(country_slug: str, subgraph_name: str, continent: str, overwrite: bool) -> dict:
    """
    Module-level worker function for multiprocessing.Pool.
    Generates a single subgraph pickle.

    Must be module-level for pickle serialization.
    """
    import django
    django.setup()

    from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
    import logging

    logger = logging.getLogger(__name__)

    try:
        service = GeoVectorsEncoderService()
        res = service.generate_subgraph_pickle(country_slug, subgraph_name, continent=continent, overwrite=overwrite)
        return res
    except Exception as e:
        logger.error(f"Worker failed for {country_slug}/{subgraph_name}: {e}", exc_info=True)
        return {
            "success": False,
            "country": country_slug,
            "subgraph": subgraph_name,
            "error": str(e),
        }


class Command(BaseCommand):
    help = (
        "Generate GeoVectors pickles for specific subgraphs (e.g., cities/admin divisions) "
        "within a country, using the Wikidata hierarchy cache to resolve subgraph names. "
        "Pickles are stored under OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/pickles/{subgraph}/wdw.pickle."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--isos",
            type=str,
            required=True,
            help="Comma-separated list of ISO codes (e.g., IE,FR).",
        )
        parser.add_argument(
            "--continent",
            type=str,
            help="Optional continent (e.g., europe, africa). Auto-resolved if not provided.",
        )
        parser.add_argument(
            "--subgraphs",
            type=str,
            help=(
                "Comma-separated list of subgraph names to process "
                "(e.g., Dublin,Paris,London). If omitted, --max-subgraphs is used."
            ),
        )
        parser.add_argument(
            "--max-subgraphs",
            type=int,
            default=None,
            help="If --subgraphs is omitted, process the first N subgraphs from the hierarchy cache.",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=4,
            help="Number of parallel worker processes (default: 4).",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Regenerate existing pickle files instead of skipping them.",
        )

    def handle(self, *args, **options):
        isos_str = options["isos"]
        continent = options.get("continent")
        subgraphs_str = options.get("subgraphs")
        max_subgraphs = options.get("max_subgraphs")
        max_workers = options.get("workers", 4)
        overwrite = options.get("overwrite", False)

        # Load country relations from OSMWikiDataHierarchy
        country_relations = get_country_relations_dict()
        
        if not country_relations:
            self.stdout.write(
                self.style.ERROR(
                    "No country relations found in OSMWikiDataHierarchy. "
                    "Run 'import_country_relations' first."
                )
            )
            return

        # Filter by ISO codes (case-insensitive lookup)
        isos = [iso.strip().upper() for iso in isos_str.split(",") if iso.strip()]
        if len(isos) != 1:
            self.stdout.write(
                self.style.ERROR("Exactly one ISO code must be provided for subgraph pickle generation.")
            )
            return

        iso = isos[0]
        mapping = country_relations.get(iso.lower())
        if not mapping:
            # Try case-insensitive search
            for k, v in country_relations.items():
                if k.upper() == iso:
                    mapping = v
                    break

        if not mapping:
            self.stdout.write(
                self.style.ERROR(f"ISO {iso} not found in OSMWikiDataHierarchy.")
            )
            return

        country_name = mapping.get("name")

        # Derive country_slug using JSON-based ISO overrides where needed
        default_slug = mapping.get("slug", normalize_country_slug(country_name))
        country_slug = get_country_slug(iso, default_slug)

        # Resolve subgraphs from SubgraphProfile (DB-driven)
        from django.db import models
        from orchestration.models import CountryPipelineProfile, SubgraphProfile

        try:
            country_profile = (
                CountryPipelineProfile.objects.filter(
                    models.Q(iso2__iexact=iso)
                    | models.Q(iso3__iexact=iso)
                    | models.Q(canonical_slug__iexact=country_slug)
                ).first()
            )
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(
                    f"Failed to resolve CountryPipelineProfile for {iso}: {exc}"
                )
            )
            return

        if not country_profile:
            self.stdout.write(
                self.style.ERROR(
                    f"No CountryPipelineProfile found for {iso} ({country_slug})."
                )
            )
            return

        subgraphs_qs = SubgraphProfile.objects.filter(
            country_profile=country_profile,
            has_subgraph_pbf=True,
        )

        # Filter by specific subgraphs if provided
        if subgraphs_str:
            requested = {
                normalize_country_slug(s.strip())
                for s in subgraphs_str.split(",")
                if s.strip()
            }
            subgraphs_qs = subgraphs_qs.filter(
                models.Q(slug__in=requested)
                | models.Q(name__in=[s.strip() for s in subgraphs_str.split(",") if s.strip()])
            )

        if max_subgraphs is not None:
            subgraphs_qs = subgraphs_qs.order_by("slug")[:max_subgraphs]

        subgraph_list = list(subgraphs_qs)
        if not subgraph_list:
            self.stdout.write(self.style.WARNING("No subgraphs to process for this country."))
            return

        target_subgraphs = [sg.slug for sg in subgraph_list]

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Generating subgraph pickles for {country_slug} ({len(target_subgraphs)} subgraphs) with {max_workers} workers..."
            )
        )

        # Prepare tasks for multiprocessing
        tasks = [(country_slug, subgraph, continent, overwrite) for subgraph in target_subgraphs]

        # Close connections in PARENT before fork to ensure children don't inherit FDs
        connections.close_all()

        # Process in parallel
        start_time = time.time()

        with Pool(processes=max_workers) as pool:
            results = pool.starmap(_generate_subgraph_pickle_worker, tasks)

        duration = time.time() - start_time

        # Analyze results
        success_count = 0
        failure_count = 0
        skipped_count = 0

        for subgraph_name, res in zip(target_subgraphs, results):
            if res.get("skipped"):
                skipped_count += 1
                self.stdout.write(f"  [SKIP] {subgraph_name}")
            elif res.get("success"):
                success_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [OK] {subgraph_name}: {res.get('entities', 0)} entities at {res.get('path')}"
                    )
                )
            else:
                failure_count += 1
                self.stdout.write(
                    self.style.ERROR(f"  [FAIL] {subgraph_name}: {res.get('error')}")
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Subgraph pickle generation complete in {duration:.1f}s: "
                f"{success_count} ok, {skipped_count} skipped, {failure_count} failed."
            )
        )
