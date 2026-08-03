"""
Preprocess GSGV Embeddings -- Align with Geofabrik Naming (config-driven).

Splits multi-country GeoVectors location TSVs into per-country TSVs using a
single-pass spatial assignment engine (pyosmium + shapely). Split definitions
are read from the "embedding_splits" section in ``settings.OVERRIDES_JSON_PATH``.

This approach does NOT depend on the vectors DB being populated, so it works
even on a fresh planet reset.

Usage:
    python manage.py preprocess_embeddings --dry-run
    python manage.py preprocess_embeddings
    python manage.py preprocess_embeddings --country scotland
    python manage.py preprocess_embeddings --country malaysia

"""

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from django.conf import settings
from django.core.management.base import BaseCommand

from extraction.services.embedding_spatial_split_service import (
    EmbeddingSpatialSplitService,
    load_embedding_splits_config,
)

logger = logging.getLogger(__name__)


def _run_split_group_task(args) -> None:
    """Worker entrypoint for running a single split group in a separate process.

    Args tuple: (
        split_def: dict,
        emb_root_str: str,
        polys_root_str: str,
        continents_root_str: str,
        country_filter: Optional[str],
        force: bool,
        force_snapshot: bool,
    )
    """
    (
        split_def,
        emb_root_str,
        polys_root_str,
        continents_root_str,
        country_filter,
        force,
        force_snapshot,
    ) = args

    emb_root = Path(emb_root_str)
    polys_root = Path(polys_root_str)
    continents_root = Path(continents_root_str)

    service = EmbeddingSpatialSplitService(
        embeddings_root=emb_root,
        polygons_root=polys_root,
        continents_root=continents_root,
        stdout=None,
    )
    service.process_split_group(
        split_def,
        country_filter=country_filter,
        force=force,
        force_snapshot=force_snapshot,
    )


class Command(BaseCommand):
    help = (
        "Preprocess GSGV embeddings -- split multi-country TSVs using a "
        "single-pass spatial assignment engine (pyosmium + shapely)."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument(
            "--embeddings-root",
            type=str,
            default=None,
            help="Override EMBEDDINGS_ROOT path",
        )
        parser.add_argument(
            "--polygons-root",
            type=str,
            default=None,
            help="Override POLYGON_FILES_DIR path",
        )
        parser.add_argument(
            "--continents-root",
            type=str,
            default=None,
            help="Directory containing continent .pbf files",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--country",
            type=str,
            default=None,
            help=(
                "Only process the split for a specific country target slug "
                "(e.g. --country scotland, --country malaysia). Runs all "
                "splits if omitted."
            ),
        )
        parser.add_argument(
            "--force-snapshot",
            action="store_true",
            help="Force re-creation of continent snapshot even if cached",
        )
        parser.add_argument(
            "--keep-temp-pbfs",
            action="store_true",
            help=(
                "Deprecated flag (kept for CLI compatibility). No-op in the "
                "spatial splitter implementation."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force re-processing even if output TSV files already exist",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[override]
        emb_root = Path(options["embeddings_root"]) if options["embeddings_root"] else Path(settings.EMBEDDINGS_ROOT)
        polys_root = Path(options["polygons_root"]) if options["polygons_root"] else Path(settings.POLYGON_FILES_DIR)
        continents_root = (
            Path(options["continents_root"]) if options["continents_root"] else Path(settings.CONTINENTS_ROOT)
        )
        dry_run: bool = options["dry_run"]
        country_filter: Optional[str] = options.get("country")
        force_snapshot: bool = options["force_snapshot"]
        force: bool = options["force"]

        if not emb_root.exists():
            self.stdout.write(self.style.ERROR(f"Embeddings root not found: {emb_root}"))
            return

        if not polys_root.exists():
            self.stdout.write(self.style.ERROR(f"Polygons root not found: {polys_root}"))
            return

        if not continents_root.exists():
            self.stdout.write(self.style.ERROR(f"Continents root not found: {continents_root}"))
            return

        config = load_embedding_splits_config()
        splits: List[dict] = config.get("splits", [])

        if not splits:
            self.stdout.write(
                self.style.WARNING(
                    "No embedding splits configured (embedding_splits section missing from overrides.json).",
                ),
            )
            return

        self.stdout.write("=" * 60)
        self.stdout.write("GSGV Embedding Preprocessing (Spatial, config-driven)")
        self.stdout.write(f"  Embeddings: {emb_root}")
        self.stdout.write(f"  Polygons:   {polys_root}")
        self.stdout.write(f"  Continents: {continents_root}")
        self.stdout.write(f"  Dry run:    {dry_run}")
        if country_filter:
            self.stdout.write(f"  Country filter: {country_filter}")
        self.stdout.write("=" * 60)

        # Fail fast: validate all inputs (source TSVs + poly files) before work
        errors: List[str] = []
        for split_def in splits:
            src_rel = split_def.get("source_tsv")
            continent = split_def.get("continent")
            targets = split_def.get("targets") or []
            if not src_rel or not continent or not targets:
                errors.append(f"Invalid split definition (missing keys): {split_def}")
                continue

            src_path = emb_root / src_rel
            if not src_path.exists():
                errors.append(f"Source TSV not found: {src_path}")

            for t in targets:
                slug = t.get("slug")
                poly_name = t.get("poly")
                if not slug or not poly_name:
                    errors.append(f"Invalid target entry in split {src_rel}: {t}")
                    continue
                if country_filter and slug != country_filter:
                    continue
                poly_path = EmbeddingSpatialSplitService.resolve_poly_path(polys_root, poly_name)
                if not poly_path:
                    errors.append(
                        f"Poly file not found for target {slug}: {poly_name} "
                        f"(look under {polys_root}/overrides or {polys_root})",
                    )

        if errors:
            self.stdout.write(self.style.ERROR("Embedding splits validation failed:"))
            for msg in errors:
                self.stdout.write(self.style.ERROR(f"  - {msg}"))
            return

        service = EmbeddingSpatialSplitService(
            embeddings_root=emb_root,
            polygons_root=polys_root,
            continents_root=continents_root,
            stdout=self.stdout,
        )

        total_start = time.time()
        groups_processed = 0

        # Build list of split groups to process
        task_defs = []
        for split_def in splits:
            src_rel = split_def.get("source_tsv")
            targets = split_def.get("targets") or []
            if not src_rel or not targets:
                continue

            if country_filter and not any(t.get("slug") == country_filter for t in targets):
                continue

            src_path = emb_root / src_rel
            if not src_path.exists():
                # Already reported during validation; skip
                continue

            if dry_run:
                self.stdout.write(f"\n  [DRY-RUN] Would split {src_path} into:")
                for t in targets:
                    slug = t.get("slug")
                    if country_filter and slug != country_filter:
                        continue
                    poly_name = t.get("poly")
                    output = t.get("output")
                    self.stdout.write(f"    -> {slug} ({poly_name}) -> {output}")
            else:
                task_defs.append(split_def)

        if dry_run:
            elapsed = time.time() - total_start
            self.stdout.write(f"\n{'=' * 60}")
            self.stdout.write(
                f"Preprocessing complete in {elapsed:.1f}s (groups processed: {groups_processed})",
            )
            self.stdout.write(f"{'=' * 60}")
            return

        if not task_defs:
            elapsed = time.time() - total_start
            self.stdout.write(f"\n{'=' * 60}")
            self.stdout.write(
                f"Preprocessing complete in {elapsed:.1f}s (groups processed: {groups_processed})",
            )
            self.stdout.write(f"{'=' * 60}")
            return

        # Optional multi-core: control workers via EMBEDDING_SPLITS_WORKERS
        workers_env = os.getenv("EMBEDDING_SPLITS_WORKERS")
        try:
            workers = int(workers_env) if workers_env else 1
        except ValueError:
            workers = 1

        workers = max(1, workers)
        workers = min(workers, len(task_defs))

        if workers == 1:
            # Sequential processing (default)
            for split_def in task_defs:
                src_rel = split_def.get("source_tsv")
                self.stdout.write(f"\n  Processing split group: {src_rel}")
                try:
                    service.process_split_group(
                        split_def,
                        country_filter=country_filter,
                        force=force,
                        force_snapshot=force_snapshot,
                    )
                    groups_processed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Error while processing split group %s", src_rel)
                    self.stdout.write(
                        self.style.ERROR(f"  Split failed for {src_rel}: {exc}"),
                    )
        else:
            # Multi-process execution across split groups
            self.stdout.write(
                f"\nRunning embedding splits with {workers} worker processes...",
            )
            args_list = [
                (
                    split_def,
                    str(emb_root),
                    str(polys_root),
                    str(continents_root),
                    country_filter,
                    force,
                    force_snapshot,
                )
                for split_def in task_defs
            ]

            with ProcessPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(_run_split_group_task, args): args[0].get("source_tsv")
                    for args in args_list
                }
                for future in as_completed(future_map):
                    src_rel = future_map[future]
                    try:
                        future.result()
                        groups_processed += 1
                        self.stdout.write(f"  [OK] {src_rel}")
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Error while processing split group %s", src_rel)
                        self.stdout.write(
                            self.style.ERROR(f"  Split failed for {src_rel}: {exc}"),
                        )

        elapsed = time.time() - total_start
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(
            f"Preprocessing complete in {elapsed:.1f}s (groups processed: {groups_processed})",
        )
        self.stdout.write(f"{'=' * 60}")
