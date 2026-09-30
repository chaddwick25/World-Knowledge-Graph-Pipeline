"""
Train GV-NLE Spatial Embeddings

Management command to train GV-NLE embeddings for OSM entities using:
1. k-NN graph construction (k=50, haversine distance)
2. Weighted DeepWalk (random walks with edge weight sampling)
3. Skip-Gram training (Word2Vec-style)

The pipeline body lives in ``GVNLETrainingService``
(``semantic_search/services/gv_nle_training_service.py``); this command is
a thin argument-parsing wrapper (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).

Usage:
    python manage.py train_gv_nle --region=gibraltar --k=50 --workers=8
"""

from django.core.management.base import BaseCommand

from semantic_search.services.gv_nle_training_service import GVNLETrainingService


class Command(BaseCommand):
    help = 'Train GV-NLE spatial embeddings using k-NN graph + weighted DeepWalk'

    def add_arguments(self, parser):
        parser.add_argument(
            '--region',
            type=str,
            help='Region filter (e.g., "gibraltar", "canada")',
        )
        parser.add_argument(
            '--isos',
            type=str,
            default=None,
            metavar='CODE',
            help='ISO 3166-1 alpha-2 country code for subgraph processing (e.g., MZ, IE). Processes all subgraphs in parallel if hierarchy exists.',
        )
        parser.add_argument(
            '--k',
            type=int,
            default=50,
            help='Number of nearest neighbors (default: 50)',
        )
        parser.add_argument(
            '--embedding-dim',
            type=int,
            default=100,
            help='Embedding dimension (default: 100, per GeoVectors-master)',
        )
        parser.add_argument(
            '--walk-length',
            type=int,
            default=80,
            help='Random walk length (default: 80)',
        )
        parser.add_argument(
            '--num-walks',
            type=int,
            default=10,
            help='Number of walks per node (default: 10)',
        )
        parser.add_argument(
            '--window-size',
            type=int,
            default=5,
            help='Skip-Gram window size (default: 5, per GeoVectors paper §5.1)',
        )
        parser.add_argument(
            '--epochs',
            type=int,
            default=1,
            help='Training epochs (default: 1; walks provide diversity, single pass suffices)',
        )
        parser.add_argument(
            '--negative',
            type=int,
            default=5,
            help='Negative samples per positive (default: 5, per Mikolov et al. 2013)',
        )
        parser.add_argument(
            '--sample',
            type=float,
            default=1e-5,
            help='Subsampling threshold (default: 1e-5; lower for graph node IDs)',
        )
        parser.add_argument(
            '--ns-exponent',
            type=float,
            default=0.75,
            help='Negative sampling distribution exponent (default: 0.75)',
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=26,
            help='Number of parallel workers (default: 26, leveraging 26-core E-cluster)',
        )
        parser.add_argument(
            '--save-graph',
            action='store_true',
            help='Save k-NN graph to file',
        )
        parser.add_argument(
            '--save-model',
            action='store_true',
            help='Save trained DeepWalk model',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=10000,
            help='Batch size for database updates (default: 10000)',
        )
        parser.add_argument(
            '--source-snapshot',
            type=str,
            help='UUID of source TemporalSnapshot to link entities to',
        )
        parser.add_argument(
            '--snapshot-date',
            type=str,
            default=None,
            help='Phase 6 partition key (YYYY_MM_DD) for partition pruning',
        )
        parser.add_argument(
            '--country',
            type=str,
            default=None,
            metavar='CODE',
            help=(
                'ISO 3166-1 alpha-2 country code for geofenced training '
                '(e.g. DE, GB, TZ). Builds k-NN graph from buffered country '
                'polygon; writes embeddings back only for entities strictly '
                'inside the country boundary.'
            ),
        )
        parser.add_argument(
            '--poly-file',
            type=str,
            default=None,
            metavar='PATH',
            help='Path to .poly boundary file for exact country polygon (overrides --country bbox lookup).',
        )
        parser.add_argument(
            '--buffer-deg',
            type=float,
            default=0.45,
            help=(
                'Buffer in degrees added around the strict country polygon '
                'when loading entities for k-NN graph construction. '
                'Buffer entities anchor border nodes correctly but are not '
                'written back. Default 0.45 deg ≈ 50 km at mid-latitudes.'
            ),
        )
        parser.add_argument(
            '--gpu',
            action='store_true',
            help='Enable GPU acceleration using PyTorch Geometric (recommended)',
        )
        parser.add_argument(
            '--gpu-device',
            type=str,
            default='cuda:0',
            help='GPU device to use (default: cuda:0, options: cuda:0, cuda:1, cuda:2 for K80)',
        )
        parser.add_argument(
            '--use-pyg',
            action='store_true',
            default=True,
            help='Use PyTorch Geometric for GPU training (default: True, memory-safe)',
        )

    def handle(self, *args, **options):
        service = GVNLETrainingService(stdout=self.stdout)
        return service.run(**options)
