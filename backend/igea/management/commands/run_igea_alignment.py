"""
Iterative Entity Alignment (simplified IGEA)

Runs the simplified IGEA pipeline to extend OSM→Wikidata entity alignment
using an iterative cosine-similarity-based approach.

Based on:
    Dsouza, Yu, Windoffer, Demidova — "Iterative Geographic Entity Alignment
    with Cross-Attention", ISWC 2023.

The seed alignment is derived from OSM entities that already carry a `wikidata=`
tag.  Each iteration accepts new alignments (cosine ≥ threshold, within 2500m,
same wkg_class) and adds them to the seed for the next iteration.

Usage:
    python manage.py iterative_entity_alignment
    python manage.py iterative_entity_alignment --country DE --iterations 3
    python manage.py iterative_entity_alignment --dry-run
"""

# TODO: Refactor and take notes
from django.core.management.base import BaseCommand
from igea.services.iterative_alignment_service import (
    IterativeEntityAlignmentService,
)
from worldkg_nca.services.wikidata_service import parse_poly_to_wkt
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run simplified IGEA iterative entity alignment (Dsouza et al., ISWC 2023)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--country',
            type=str,
            default=None,
            help='ISO country code to scope processing (e.g. DE, GB, US)',
        )
        parser.add_argument(
            '--iterations',
            type=int,
            default=3,
            help='Maximum alignment iterations (default: 3, paper optimal)',
        )
        parser.add_argument(
            '--threshold',
            type=float,
            default=0.5,
            help='Cosine similarity acceptance threshold tha (default: 0.6)',
        )
        parser.add_argument(
            '--max-distance',
            type=float,
            default=2500.0,
            help='Maximum distance in metres for spatial blocking (default: 2500)',
        )
        parser.add_argument(
            '--candidate-limit',
            type=int,
            default=100_000,
            help='Max Wikidata candidate entities to load (default: 100000)',
        )
        parser.add_argument(
            '--candidate-cache',
            type=str,
            default=None,
            help='Path to Step 6 Wikidata harvest JSON cache',
        )
        parser.add_argument(
            '--poly-file',
            type=str,
            default=None,
            help='Path to .poly file for exact spatial filtering (replaces --country)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run alignment scoring but do not write results to DB',
        )

    def handle(self, *args, **options):
        country = options['country']
        iterations = options['iterations']
        threshold = options['threshold']
        max_distance = options['max_distance']
        candidate_limit = options['candidate_limit']
        dry_run = options['dry_run']

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"IGEA Iterative Entity Alignment\n"
            f"  country={country or 'all'}, iterations={iterations}\n"
            f"  threshold={threshold}, max_distance={max_distance}m\n"
            f"{'='*60}\n"
        ))

        service = IterativeEntityAlignmentService(
            max_iterations=iterations,
            threshold=threshold,
            max_distance_m=max_distance,
        )

        # Load Wikidata candidates
        pool_size = 0
        candidate_cache = options.get('candidate_cache')
        
        if candidate_cache:
            self.stdout.write(f"[1/2] Loading Wikidata candidate pool from cache: {candidate_cache}")
            pool_size = service.load_wikidata_candidates_from_file(candidate_cache)
        else:
            self.stdout.write("[1/2] Loading Wikidata candidate pool from DB...")
            pool_size = service.load_wikidata_candidates_from_db(limit=candidate_limit)
        
        self.stdout.write(self.style.SUCCESS(f"  ✓ Wikidata pool: {pool_size:,} candidates"))
        
        # Generate embeddings for Wikidata candidates if missing
        from semantic_search.services.fasttext_service import FastTextEmbeddingService
        ft_service = FastTextEmbeddingService()
        candidates_without_emb = sum(1 for c in service._wikidata_pool if not c.get('embedding'))
        
        if candidates_without_emb > 0:
            self.stdout.write(f"  → Generating embeddings for {candidates_without_emb:,} Wikidata candidates...")
            for candidate in service._wikidata_pool:
                if not candidate.get('embedding'):
                    # Generate embedding from label (similar to OSM tags)
                    label = candidate.get('label', '')
                    if label:
                        # Treat label as a single tag with count=1
                        tag_counts = {label.lower(): 1}
                        embedding = ft_service.calculate_embedding(tag_counts)
                        if embedding is not None:
                            candidate['embedding'] = embedding.tolist()
            
            enriched = sum(1 for c in service._wikidata_pool if c.get('embedding'))
            self.stdout.write(self.style.SUCCESS(f"  ✓ Enriched {enriched:,} candidates with embeddings"))

        if pool_size == 0:
            self.stdout.write(self.style.WARNING(
                "  No Wikidata candidates found.\n"
                "  Candidates are derived from OsmEntity rows that already have\n"
                "  a wikidata_uri set (wikidata= OSM tag or prior IGEA run).\n"
                "  Ensure at least some entities have a Wikidata link before running."
            ))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "[dry-run] Alignment would run but DB writes are disabled.\n"
                "  Remove --dry-run to persist results."
            ))
            return

        # Resolve polygon WKT if poly_file is provided
        polygon_wkt = None
        poly_file = options.get('poly_file')
        if poly_file:
            self.stdout.write(f"Parsing boundary from {poly_file}...")
            polygon_wkt = parse_poly_to_wkt(poly_file)
            if not polygon_wkt:
                self.stdout.write(self.style.ERROR(f"Failed to parse .poly file: {poly_file}"))
                return

        # Run iterative alignment
        self.stdout.write(f"[2/2] Running {iterations}-iteration alignment loop...")
        stats = service.run(
            country_code=country,
            polygon_wkt=polygon_wkt,
            buffer_deg=0.05  # Slight buffer to include entities on the border
        )

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ IGEA Complete\n"
            f"  Iterations run:      {stats['iterations_run']}\n"
            f"  Total accepted:      {stats['total_accepted']:,}\n"
            f"  Per-iteration:       {stats['per_iteration_counts']}\n"
            f"  Final seed size:     {stats['final_seed_size']:,}\n"
            f"  Threshold used:      {stats['threshold']}\n"
            f"  Max distance:        {stats['max_distance_m']}m\n"
        ))

        self.stdout.write(self.style.SUCCESS(
            f"{'='*60}\n"
            f"Run 'predict_spatial_links' next to fill WorldKG object-property triples.\n"
            f"{'='*60}\n"
        ))
