"""
Harvest Wikidata Geographic Candidates

Queries the Wikidata SPARQL endpoint for geographic entities within a
country or bounding box, then loads them as IGEA alignment candidates.

This command closes the closed-loop gap in the IGEA pipeline: without an
external harvest, load_wikidata_candidates_from_db() can only recycle
entities that are already linked, meaning no genuinely new links can be
discovered in regions where nobody has manually added wikidata= OSM tags.

Usage:
    # Harvest for Germany
    poetry run python manage.py harvest_wikidata_candidates --country DE

    # Harvest and cache to JSON for reuse (avoids re-querying Wikidata)
    poetry run python manage.py harvest_wikidata_candidates \
        --country DE --cache-file data/wikidata_cache/de_candidates.json

    # Load from existing cache and run IGEA immediately
    poetry run python manage.py harvest_wikidata_candidates \
        --country DE \
        --cache-file data/wikidata_cache/de_candidates.json \
        --run-igea

    # Restrict to a specific WorldKG class
    poetry run python manage.py harvest_wikidata_candidates \
        --country DE --wkg-class wkgs:Restaurant

    # Harvest by explicit bounding box
    poetry run python manage.py harvest_wikidata_candidates \
        --bbox 5.87,47.27,15.04,55.06

    # Dry run (harvest only, print class breakdown, no IGEA)
    poetry run python manage.py harvest_wikidata_candidates \
        --country DE --dry-run

Reference:
    WorldKG project:  https://www.vgiscience.org/projects/worldkg.html
    IGEA paper:       https://arxiv.org/pdf/2303.15271.pdf  (Algorithm 1)
    NCA paper:        https://arxiv.org/pdf/2107.13257.pdf  (§4.3 reverse mapping)
"""

import json
import logging
import os
import numpy as np
from collections import Counter

from django.core.management.base import BaseCommand

from semantic_search.services.wikidata_candidate_service import WikidataCandidateService
from semantic_search.services.worldkg_ontology_service import WorldKGOntologyService
from igea.services.iterative_alignment_service import (
    IterativeEntityAlignmentService,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Harvest Wikidata geographic candidates for IGEA entity alignment "
        "(WorldKG/OSM2KG pipeline — Dsouza et al.)"
    )

    def add_arguments(self, parser):
        # Geographic scope
        parser.add_argument(
            "--country",
            type=str,
            default=None,
            metavar="CODE",
            help="ISO 3166-1 alpha-2 country code (e.g. DE, GB, TZ, NG)",
        )
        parser.add_argument(
            "--bbox",
            type=str,
            default=None,
            metavar="min_lon,min_lat,max_lon,max_lat",
            help="Explicit bounding box (WGS84). Overrides --country.",
        )
        parser.add_argument(
            "--poly-file",
            type=str,
            default=None,
            metavar="PATH",
            help="Path to .poly boundary file for exact country bbox extraction.",
        )

        # Filtering
        parser.add_argument(
            "--wkg-class",
            type=str,
            default=None,
            metavar="wkgs:X",
            help="Restrict harvest to one WorldKG class (e.g. wkgs:Restaurant).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50_000,
            help="Maximum candidates to harvest per run (default: 50000).",
        )

        # Caching
        parser.add_argument(
            "--cache-file",
            type=str,
            default=None,
            metavar="PATH",
            help=(
                "JSON file path for candidate cache. "
                "If the file exists it is loaded instead of re-querying Wikidata. "
                "If it does not exist the harvest result is saved there."
            ),
        )

        # Execution mode
        parser.add_argument(
            "--enrich-classes",
            action="store_true",
            help=(
                "Run phase-2 P31 enrichment after harvesting to populate wkg_class. "
                "Sends one SPARQL VALUES query per 500 candidates (~1 req/s). "
                "Without this flag all candidates have wkg_class=None, which is "
                "fine for IGEA (falls back to distance-only blocking)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Harvest and report candidates without running IGEA.",
        )
        parser.add_argument(
            "--run-igea",
            action="store_true",
            help="Run IGEA alignment immediately after harvesting.",
        )

        # IGEA options (only used when --run-igea is set)
        parser.add_argument(
            "--igea-iterations",
            type=int,
            default=3,
            help="IGEA max iterations (default: 3, paper-optimal).",
        )
        parser.add_argument(
            "--igea-threshold",
            type=float,
            default=0.7,
            help="IGEA cosine acceptance threshold tha (default: 0.7).",
        )

    def handle(self, *args, **options):
        country   = options["country"]
        bbox_str  = options["bbox"]
        poly_file = options["poly_file"]
        wkg_class = options["wkg_class"]
        limit     = options["limit"]
        cache_file = options["cache_file"]
        dry_run   = options["dry_run"]
        run_igea  = options["run_igea"]

        if not country and not bbox_str and not poly_file:
            self.stderr.write(self.style.ERROR(
                "Provide --country CODE or --bbox min_lon,min_lat,max_lon,max_lat or --poly-file PATH\n"
                "Example:  manage.py harvest_wikidata_candidates --country DE\n"
                "Example:  manage.py harvest_wikidata_candidates --bbox 5.87,47.27,15.04,55.06"
            ))
            return

        scope_label = f"country:{country}" if country else f"bbox:{bbox_str}"
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*62}\n"
            f"Wikidata Candidate Harvest\n"
            f"  scope={scope_label}  wkg_class={wkg_class or 'all'}  limit={limit:,}\n"
            f"{'='*62}\n"
        ))

        # ── Step 1: Obtain candidate list ────────────────────────────────────
        candidates = []

        if cache_file and os.path.exists(cache_file):
            self.stdout.write(f"[1/2] Loading candidates from cache: {cache_file}")
            with open(cache_file, "r") as f:
                candidates = json.load(f)
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ Loaded {len(candidates):,} cached candidates"
            ))

            # Compute embeddings if running IGEA (cache load path)
            if candidates and run_igea:
                self.stdout.write(
                    "[1b/2] Computing FastText embeddings for candidate labels..."
                )
                from semantic_search.services.model_registry_service import ModelRegistryService

                model = ModelRegistryService.get_fasttext_model()
                embedded_count = 0
                for c in candidates:
                    label = c.get('label', '')
                    if label:
                        # Use the label directly as a tag to get its FastText embedding
                        vec = model.get_word_vector(label)
                        # L2 normalize
                        norm = np.linalg.norm(vec)
                        if norm > 0:
                            vec = vec / norm
                        c['embedding'] = vec.tolist()
                        embedded_count += 1
                    else:
                        c['embedding'] = None

                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Computed embeddings for {embedded_count:,}/{len(candidates):,} candidates"
                ))
        else:
            self.stdout.write("[1/2] Querying Wikidata SPARQL endpoint...")
            self.stdout.write(
                "  Note: rate-limited to ~1 req/s — large areas may take several minutes."
            )

            ontology_service = WorldKGOntologyService()
            service = WikidataCandidateService(ontology_service=ontology_service)

            if bbox_str:
                try:
                    parts = [float(x) for x in bbox_str.split(",")]
                    if len(parts) != 4:
                        raise ValueError
                    min_lon, min_lat, max_lon, max_lat = parts
                except ValueError:
                    self.stderr.write(self.style.ERROR(
                        f"Invalid --bbox: '{bbox_str}'. "
                        "Expected four comma-separated floats: min_lon,min_lat,max_lon,max_lat"
                    ))
                    return
                candidates = service.harvest_by_bbox(
                    min_lon, min_lat, max_lon, max_lat,
                    wkg_class_filter=wkg_class,
                    limit=limit,
                )
            else:
                candidates = service.harvest_by_country(
                    country_code=country,
                    wkg_class_filter=wkg_class,
                    limit=limit,
                    poly_file_path=poly_file,
                )

            self.stdout.write(self.style.SUCCESS(
                f"  ✓ Harvested {len(candidates):,} candidates from Wikidata"
            ))

            # Optional phase-2 P31 enrichment
            if options.get("enrich_classes") and candidates:
                n_batches = (len(candidates) + 499) // 500
                self.stdout.write(
                    f"[1b/2] Enriching wkg_class via P31/P279* lookups "
                    f"({n_batches} batches of 500, ~{n_batches}s)..."
                )
                service.enrich_wkg_class(candidates)
                mapped = sum(1 for c in candidates if c.get("wkg_class"))
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ wkg_class mapped for {mapped:,}/{len(candidates):,} candidates"
                ))
            elif not options.get("enrich_classes"):
                self.stdout.write(
                    "  (wkg_class=None for all; add --enrich-classes for P31 mapping)"
                )

            # Phase-3: Compute FastText embeddings for candidate labels (required for IGEA)
            if candidates and run_igea:
                self.stdout.write(
                    "[1c/2] Computing FastText embeddings for candidate labels..."
                )
                from semantic_search.services.fasttext_service import FastTextEmbeddingService
                from semantic_search.services.model_registry_service import ModelRegistryService

                model = ModelRegistryService.get_fasttext_model()
                embedded_count = 0
                for c in candidates:
                    label = c.get('label', '')
                    if label:
                        # Use the label directly as a tag to get its FastText embedding
                        vec = model.get_word_vector(label)
                        # L2 normalize
                        norm = np.linalg.norm(vec)
                        if norm > 0:
                            vec = vec / norm
                        c['embedding'] = vec.tolist()
                        embedded_count += 1
                    else:
                        c['embedding'] = None

                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Computed embeddings for {embedded_count:,}/{len(candidates):,} candidates"
                ))

            # Save to cache
            if cache_file:
                cache_dir = os.path.dirname(cache_file)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                with open(cache_file, "w") as f:
                    json.dump(candidates, f)
                self.stdout.write(self.style.SUCCESS(f"  ✓ Saved to cache: {cache_file}"))

        if not candidates:
            self.stdout.write(self.style.WARNING(
                "No candidates harvested. Check country code, bbox, or SPARQL endpoint availability."
            ))
            return

        # ── Class breakdown ──────────────────────────────────────────────────
        class_counts = Counter(c.get("wkg_class") or "unmapped" for c in candidates)
        self.stdout.write(f"\nTop wkgs: classes in harvested candidates:")
        for cls, cnt in class_counts.most_common(12):
            bar = "█" * min(30, cnt // max(1, max(class_counts.values()) // 30))
            self.stdout.write(f"  {cls:<40} {cnt:>6,}  {bar}")

        unmapped_pct = 100 * class_counts.get("unmapped", 0) / len(candidates)
        if unmapped_pct > 50:
            self.stdout.write(self.style.WARNING(
                f"\n  ⚠ {unmapped_pct:.0f}% of candidates have no wkgs: mapping.\n"
                "  Possible causes:\n"
                "    1. Ontology not loaded: manage.py enrich_worldkg_classes "
                "--load-ontology data/worldkg_ontology_sample.json\n"
                "    2. Wikidata SPARQL rate-limiting (check batch warnings above)\n"
                "    3. Candidates have P31 types outside the ontology coverage"
            ))

        # ── Step 2: Optionally run IGEA ──────────────────────────────────────
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\n[dry-run] Candidates ready but IGEA not run. "
                "Drop --dry-run and add --run-igea to run alignment."
            ))
            return

        if not run_igea:
            scope_arg = (
                f"--country {country}" if country
                else f"--bbox {bbox_str}" if bbox_str
                else f"--poly-file {poly_file}"
            )
            self.stdout.write(
                "\nCandidates harvested. To run IGEA now:\n"
                f"  manage.py harvest_wikidata_candidates "
                f"{scope_arg} "
                f"--cache-file <path> --run-igea\n"
                "Or load candidates programmatically:\n"
                "  igea = IterativeEntityAlignmentService()\n"
                "  igea.load_wikidata_candidates(candidates)\n"
                "  igea.run(country_code=country)"
            )
            return

        self.stdout.write(f"\n[2/2] Running IGEA ({options['igea_iterations']} iterations)...")
        igea = IterativeEntityAlignmentService(
            max_iterations=options["igea_iterations"],
            threshold=options["igea_threshold"],
        )
        igea.load_wikidata_candidates(candidates)
        stats = igea.run(country_code=country)

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ IGEA complete\n"
            f"  Iterations run:  {stats['iterations_run']}\n"
            f"  Total accepted:  {stats['total_accepted']:,}\n"
            f"  Per-iteration:   {stats['per_iteration_counts']}\n"
            f"  Final seed size: {stats['final_seed_size']:,}\n"
            f"\nRun 'predict_spatial_links' next to fill WorldKG object-property triples.\n"
            f"{'='*62}\n"
        ))
