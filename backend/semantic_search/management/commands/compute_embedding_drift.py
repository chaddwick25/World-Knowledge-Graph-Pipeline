"""Django management command to compute embedding-level Wasserstein drift and freshness score."""

import logging
from django.core.management.base import BaseCommand
from semantic_search.services.embedding_drift_service import get_embedding_drift_service

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Compute Sliced Wasserstein Distance (SWD) and freshness score between snapshot embeddings.

    Uses 300D semantic (GV-Tags) and 100D spatial (GV-NLE) embeddings.
    """

    help = "Compute Sliced Wasserstein Distance (SWD) and freshness score between snapshot embeddings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            type=str,
            default="BZ",
            help="ISO 3166-1 alpha-2 country code (default: BZ)",
        )
        parser.add_argument(
            "--snapshot-from",
            type=str,
            default="2025_12_31_baseline",
            help="Earlier snapshot partition key (default: 2025_12_31_baseline)",
        )
        parser.add_argument(
            "--snapshot-to",
            type=str,
            default="2025_12_31",
            help="Later snapshot partition key (default: 2025_12_31)",
        )
        parser.add_argument(
            "--num-projections",
            type=int,
            default=100,
            help="Number of random projections for SWD (default: 100)",
        )
        parser.add_argument(
            "--mock-missing",
            action="store_true",
            default=False,
            help="Generate synthetic embeddings if database partition is missing (recommended for testing)",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=42,
            help="Random seed for reproducibility (default: 42)",
        )

    def handle(self, *args, **options):
        country = options["country"].upper()
        snap_from = options["snapshot_from"]
        snap_to = options["snapshot_to"]
        num_projections = options["num_projections"]
        mock_missing = options["mock_missing"]
        seed = options["seed"]

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*75}\n"
            f"Embedding Drift and Freshness Assessment\n"
            f"Country: {country} | Snapshots: {snap_from} -> {snap_to}\n"
            f"{'='*75}\n"
        ))

        service = get_embedding_drift_service()

        self.stdout.write("Running Wasserstein Distance calculations...")
        try:
            results = service.compute_country_drift(
                country_code=country,
                snapshot_from=snap_from,
                snapshot_to=snap_to,
                num_projections=num_projections,
                mock_missing=mock_missing,
                seed=seed
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error computing drift: {str(e)}"))
            import traceback
            traceback.print_exc()
            return

        # Print Global Results
        g = results["global"]
        self.stdout.write(self.style.SUCCESS(
            f"\nGLOBAL METRICS:\n"
            f"  Semantic (GV-Tags, 300D) Wasserstein Distance (semantic_W) : {g['semantic_w']:.4f}\n"
            f"  Spatial  (GV-NLE,  100D) Wasserstein Distance (spatial_W)  : {g['spatial_w']:.4f}\n"
            f"  Combined Freshness Score (100% = Identical)                : {g['freshness_score']*100:.2f}%\n"
            f"  Entity counts (From -> To):\n"
            f"    Semantic: {g['semantic_count_from']} -> {g['semantic_count_to']}\n"
            f"    Spatial:  {g['spatial_count_from']} -> {g['spatial_count_to']}\n"
        ))

        # Print Subdivision Results
        subdivisions = results["subdivisions"]
        if subdivisions:
            self.stdout.write(self.style.SUCCESS(f"\nSUBDIVISION METRICS:\n"))
            self.stdout.write(f"  {'-'*78}")
            self.stdout.write(f"  {'Subdivision':<25} | {'semantic_W':<10} | {'spatial_W':<10} | {'Freshness':<10} | {'Count (From/To)':<15}")
            self.stdout.write(f"  {'-'*78}")
            for name, metrics in subdivisions.items():
                freshness_str = f"{metrics['freshness_score']*100:.1f}%"
                counts_str = f"{metrics['semantic_count_from']}/{metrics['semantic_count_to']}"
                self.stdout.write(
                    f"  {name:<25} | {metrics['semantic_w']:<10.4f} | {metrics['spatial_w']:<10.4f} | {freshness_str:<10} | {counts_str:<15}"
                )
            self.stdout.write(f"  {'-'*78}\n")
        else:
            self.stdout.write(self.style.WARNING("\nNo subdivisions/subgraphs found or computed for this country.\n"))

        self.stdout.write(self.style.SUCCESS(
            f"{'='*75}\n"
            f"Calculation Complete!\n"
            f"{'='*75}\n"
        ))
