"""Create HNSW index on OsmEntity.static_embedding for ANN search.

Sets ``max_parallel_maintenance_workers`` for the build session so the
HNSW graph construction uses parallel workers (pgvector 0.5+ supports
this). The DB is already sized for it — ``maintenance_work_mem=16GB``
and ``max_parallel_workers=8`` — but the per-session
``max_parallel_maintenance_workers`` defaults to 2. Use ``--parallel-workers``
to bump it for the build, especially for the initial backfill.

Usage::

    python manage.py create_static_embedding_hnsw_index
    python manage.py create_static_embedding_hnsw_index --parallel-workers 4
    python manage.py create_static_embedding_hnsw_index --m 32 --ef-construction 128

The ``--rebuild`` flag drops the index first (useful after a major data
reload when the graph quality has degraded).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = "Create HNSW index on static_embedding for ANN search in the vectors DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--parallel-workers", type=int, default=None,
            help=(
                "Set max_parallel_maintenance_workers for this session "
                "(pgvector HNSW build is parallel-safe). The DB default is 2; "
                "bump to 4+ for the initial backfill on large tables."
            ),
        )
        parser.add_argument(
            "--m", type=int, default=16,
            help="HNSW M parameter (max connections per node). Default: 16.",
        )
        parser.add_argument(
            "--ef-construction", type=int, default=64,
            help="HNSW ef_construction (search width during build). Default: 64.",
        )
        parser.add_argument(
            "--rebuild", action="store_true",
            help="Drop the existing index before recreating (use after major data reloads).",
        )

    def handle(self, *args, **options):
        parallel_workers = options["parallel_workers"]
        m = options["m"]
        ef_construction = options["ef_construction"]
        rebuild = options["rebuild"]

        self.stdout.write(self.style.WARNING(
            f"Creating HNSW index on static_embedding (vectors DB) "
            f"with m={m}, ef_construction={ef_construction}"
            f"{f', parallel_workers={parallel_workers}' if parallel_workers else ''}"
            f"{', rebuild=True' if rebuild else ''}..."
        ))

        with connections['vectors'].cursor() as cursor:
            # Ensure pgvector extension is available
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

            # Bump parallel maintenance workers for this session if requested.
            # This only affects the current connection — it does NOT change
            # the global server setting.
            if parallel_workers is not None:
                if parallel_workers < 0:
                    raise CommandError("--parallel-workers must be >= 0")
                cursor.execute(
                    f"SET max_parallel_maintenance_workers = {int(parallel_workers)};"
                )
                # Report the effective value so the operator can confirm.
                cursor.execute("SHOW max_parallel_maintenance_workers;")
                effective = cursor.fetchone()[0]
                self.stdout.write(f"  max_parallel_maintenance_workers = {effective}")

            if rebuild:
                self.stdout.write("  Dropping existing index (rebuild)...")
                cursor.execute(
                    "DROP INDEX IF EXISTS osmentity_static_embedding_hnsw_idx;"
                )

            # HNSW index on static_embedding (cosine distance)
            cursor.execute(f"""
                CREATE INDEX IF NOT EXISTS osmentity_static_embedding_hnsw_idx
                ON semantic_search_osmentity
                USING hnsw (static_embedding vector_cosine_ops)
                WITH (m = {int(m)}, ef_construction = {int(ef_construction)});
            """)

        self.stdout.write(self.style.SUCCESS(
            f"Created osmentity_static_embedding_hnsw_idx "
            f"with m={m}, ef_construction={ef_construction}."
        ))
