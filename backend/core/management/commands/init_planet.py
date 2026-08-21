"""``init_planet`` — Docker startup command that replaces the planet init
Celery canvas (steps 0a–0m).

Run once after ``migrate`` on a fresh database to populate:

* planet/continent/country hierarchy primitives (OSMWikiDataHierarchy,
  RegionHierarchy, PolygonFile, PbfFile)
* CountryPipelineProfile / SubgraphProfile skeletons
* resolved TSV / PBF / pickle paths
* EligibleCountry embedding-scan rows (drives map colouring)
* WorldKG ontology (loaded into Redis)
* OSM administrative boundaries

Idempotent — every step uses ``update_or_create`` / ``get_or_create`` or
calls a management command that is itself idempotent. Safe to re-run.

This command is invoked by ``docker-entrypoint.sh`` on the backend
container after migrations complete. The worker container does NOT run
it (the ``RUN_MIGRATIONS`` env var already gates which container does
startup work). A ``PlanetSnapshot`` row acts as the run-level lock —
concurrent invocations short-circuit if a snapshot for today is already
marked COMPLETED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger("pipeline")


class Command(BaseCommand):
    help = (
        "Initialize planet data — run once after migrations on a fresh DB. "
        "Idempotent: safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-continents",
            action="store_true",
            help="Skip continent PBF extraction (only safe if already done).",
        )
        parser.add_argument(
            "--skip-embeddings",
            action="store_true",
            help="Skip embedding scan/split/merge steps.",
        )
        parser.add_argument(
            "--step",
            type=str,
            default=None,
            help="Run only the named step (see --help output for the list).",
        )
        parser.add_argument(
            "--planet-pbf",
            type=str,
            default=None,
            help="Override the planet PBF path (defaults to settings.PLANET_OSM_FILE_PATH).",
        )
        parser.add_argument(
            "--no-lock",
            action="store_true",
            help="Skip the PlanetSnapshot run-lock check (use with care).",
        )

    # ──────────────────────────────────────────────────────────────────────
    # Step implementations
    # ──────────────────────────────────────────────────────────────────────

    def _register_planet(self) -> None:
        """Step 0a — register the planet PBF + Wikidata hierarchy skeleton."""
        from core.services.planet_init.planet_initialization_service import (
            PlanetInitializationService,
        )

        planet_pbf = (
            Path(self._planet_pbf)
            if self._planet_pbf
            else Path(settings.PLANET_OSM_FILE_PATH)
        )
        policy = {
            "stages": {
                "planet_initialization": {
                    "parameters": {
                        "planet_pbf_path": str(planet_pbf),
                        "extract_continents": False,
                        "pipeline_run_id": "",
                    },
                    "file_structure": {
                        "base_dir": str(Path(settings.BASE_DATA_DIR)),
                        "directories": {
                            "osm_pbf": str(Path(settings.BASE_DATA_DIR) / "OSM-PBF-FILES"),
                            "osm_wikidata": str(settings.OSM_WIKIDATA_EXTRACTIONS_DIR),
                            "polygons": str(settings.POLYGON_FILES_DIR),
                            "logs": str(settings.LOGS_DIR),
                            "embeddings": str(settings.EMBEDDINGS_ROOT),
                            "wikidata": str(settings.WIKIDATA_CACHE_DIR),
                        },
                    },
                    "planetary_metrics": {"enabled": True},
                }
            }
        }
        PlanetInitializationService(policy).execute()

    def _extract_continents(self) -> None:
        """Step 0b — extract continent PBFs from the planet file via osmium."""
        from core.services.planet_init.planet_initialization_service import (
            PlanetInitializationService,
        )

        planet_pbf = (
            Path(self._planet_pbf)
            if self._planet_pbf
            else Path(settings.PLANET_OSM_FILE_PATH)
        )
        policy = {
            "stages": {
                "planet_initialization": {
                    "parameters": {
                        "planet_pbf_path": str(planet_pbf),
                        "pipeline_run_id": "",
                    },
                    "file_structure": {"base_dir": str(Path(settings.BASE_DATA_DIR))},
                }
            }
        }
        PlanetInitializationService(policy)._extract_continents_simple()

    def _sync_hierarchy(self) -> None:
        """Step 0c — re-sync country_relations.json, re-import, pre-build profiles.

        Combines the old step_0c_prebuild_structure sub-steps: country_relation
        re-sync, ``import_country_relations``, ``sync_geovectors_metadata``,
        and ``prebuild_worldkg_structure``.
        """
        from core.services.planet_init.country_relation_resolver import (
            country_relation_resolver,
        )

        merged = country_relation_resolver.sync(force_refresh=False)
        self.stdout.write(f"  country_relations merged: {len(merged)} entries")

        call_command("import_country_relations")
        call_command("sync_geovectors_metadata", save=True)
        call_command("prebuild_worldkg_structure")

    def _prebuild_country_paths(self) -> None:
        """Step 0d — resolve TSV / PBF / pickle paths on CountryPipelineProfile."""
        call_command("prebuild_country_paths")

    def _prebuild_subgraphs(self) -> None:
        """Step 0e — generate SubgraphProfile rows from Geofabrik hierarchy."""
        call_command("prebuild_subgraphs")

    def _prebuild_wikidata_ids(self) -> None:
        """Step 0f — backfill Q-IDs on CountryPipelineProfile / SubgraphProfile."""
        call_command("prebuild_wikidata_ids")

    def _copy_gb_to_uk(self) -> None:
        """Step 0h.1 — copy great-britain TSVs to united-kingdom naming."""
        from core.services.snapshot.gb_uk_copy_service import GbToUkCopyService

        summary = GbToUkCopyService(Path(settings.EMBEDDINGS_ROOT)).run()
        self.stdout.write(
            "  GB→UK copy: copied={copied} skipped={skipped} failed={failed}".format(
                **summary
            )
        )

    def _scan_embeddings(self) -> None:
        """Step 0h.2 — scan EMBEDDINGS_ROOT, populate EligibleCountry rows."""
        call_command("scan_embeddings", clear=True)

    def _split_embeddings(self) -> None:
        """Step 0i — split multi-country TSVs (GB, MY/SG/BN) into per-country TSVs."""
        from core.services.snapshot.embedding_split_service import EmbeddingSplitService

        continents_root = (
            Path(settings.CONTINENTS_ROOT)
            if getattr(settings, "CONTINENTS_ROOT", None)
            else Path(settings.BASE_DATA_DIR)
            / "OSM-PBF-FILES"
            / "osm_wikidata_extractions"
            / "continents"
            if settings.BASE_DATA_DIR
            else Path("/app/data/OSM-PBF-FILES/osm_wikidata_extractions/continents")
        )
        summary = EmbeddingSplitService(
            embeddings_root=Path(settings.EMBEDDINGS_ROOT),
            continents_root=continents_root,
            polygons_root=Path(settings.POLYGON_FILES_DIR),
        ).run()
        self.stdout.write(
            "  split: split={split} skipped={skipped} failed={failed}".format(**summary)
        )

    def _merge_us_embeddings(self) -> None:
        """Step 0j — merge 5 US regional shards into single US TSVs."""
        from core.services.snapshot.embedding_merge_service import EmbeddingMergeService

        summary = EmbeddingMergeService(Path(settings.EMBEDDINGS_ROOT)).run()
        self.stdout.write(
            "  US merge: status={status} elapsed={elapsed_s:.0f}s".format(**summary)
        )

    def _rescan_embeddings(self) -> None:
        """Step 0k — re-scan after split/merge so EligibleCountry reflects READY."""
        call_command("scan_embeddings", clear=True)

    def _enrich_worldkg_classes(self) -> None:
        """Step 0l — load the WorldKG ontology TTL into Redis for fast lookup."""
        call_command(
            "enrich_worldkg_classes",
            "--load-from-ttl",
            settings.WORLDKG_ONTOLOGY_PATH,
        )

    def _generate_osm_boundaries(self) -> None:
        """Step 0m — generate OSM administrative boundaries for cartography."""
        call_command("generate_osm_boundaries")

    def _train_mapqa_parser(self) -> None:
        """Step 0m.5 — train MapQA TF-IDF parser + serialize artifacts.

        Trains the template classifier, concept extractor, and role assigner
        on the MapQA dataset. Auto-generates the training CSV from the raw
        dataset if it doesn't exist. Artifacts are written to
        {MAPQA_PARSER_DATA_DIR}/artifacts/ and consumed by the next step
        (compute_amenity_embeddings) and at runtime by the MapQA executor.
        Skips gracefully if the raw dataset is not mounted.
        """
        call_command("train_mapqa_parser")

    def _compute_amenity_embeddings(self) -> None:
        """Step 0n — precompute FastText embeddings for the amenity vocabulary.

        Writes factor_amenity_embedding rows so the MapQA executor's
        semantic fallback tier is a row lookup instead of a runtime
        FastText call (FACTOR_NODE_RUNTIME_JOINS_PLAN.md §3.4).
        Skips gracefully when the parser artifacts don't exist yet.
        """
        call_command("compute_amenity_embeddings")

    def _finalize(self) -> None:
        """Mark the PlanetSnapshot row as COMPLETED.

        Optional — the planet init is best-effort. Failed steps log warnings
        but don't block the rest of the chain (matches the old Celery chain
        semantics where each step had max_retries=1).
        """
        from core.models import PlanetSnapshot

        today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
        snapshot, _ = PlanetSnapshot.objects.update_or_create(
            snapshot_date_str=today,
            defaults={
                "snapshot_date": datetime.now(timezone.utc).date(),
                "planet_osm_path": self._planet_pbf or settings.PLANET_OSM_FILE_PATH,
                "status": PlanetSnapshot.SnapshotStatus.COMPLETED,
                "completed_at": datetime.now(timezone.utc),
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"PlanetSnapshot {snapshot.snapshot_date_str} marked COMPLETED"
            )
        )

    # ──────────────────────────────────────────────────────────────────────
    # Orchestration
    # ──────────────────────────────────────────────────────────────────────

    def _all_steps(self) -> Iterable[Tuple[str, Callable[[], None]]]:
        return [
            ("register_planet", self._register_planet),
            ("extract_continents", self._extract_continents),
            ("sync_hierarchy", self._sync_hierarchy),
            ("prebuild_country_paths", self._prebuild_country_paths),
            ("prebuild_subgraphs", self._prebuild_subgraphs),
            ("prebuild_wikidata_ids", self._prebuild_wikidata_ids),
            ("copy_gb_to_uk", self._copy_gb_to_uk),
            ("scan_embeddings", self._scan_embeddings),
            ("split_embeddings", self._split_embeddings),
            ("merge_us_embeddings", self._merge_us_embeddings),
            ("rescan_embeddings", self._rescan_embeddings),
            ("enrich_worldkg_classes", self._enrich_worldkg_classes),
            ("generate_osm_boundaries", self._generate_osm_boundaries),
            ("train_mapqa_parser", self._train_mapqa_parser),
            ("compute_amenity_embeddings", self._compute_amenity_embeddings),
            ("finalize", self._finalize),
        ]

    def _step_filter(self, name: str) -> bool:
        if self._only_step is not None:
            return name == self._only_step
        if self._skip_continents and name == "extract_continents":
            return False
        if self._skip_embeddings and name in {
            "copy_gb_to_uk",
            "scan_embeddings",
            "split_embeddings",
            "merge_us_embeddings",
            "rescan_embeddings",
        }:
            return False
        return True

    def _acquire_lock(self) -> bool:
        """Return True if this command should proceed.

        Uses PlanetSnapshot as a soft lock — if a COMPLETED row exists for
        today, we short-circuit unless --no-lock was passed. This prevents
        two backend containers from running init_planet concurrently.
        """
        if self._no_lock:
            return True
        from core.models import PlanetSnapshot

        today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
        completed = PlanetSnapshot.objects.filter(
            snapshot_date_str=today,
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        ).exists()
        if completed:
            self.stdout.write(
                self.style.WARNING(
                    f"PlanetSnapshot for {today} is already COMPLETED — skipping. "
                    "Pass --no-lock to force re-run."
                )
            )
            return False
        return True

    # ──────────────────────────────────────────────────────────────────────
    # handle()
    # ──────────────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        self._planet_pbf = options["planet_pbf"]
        self._skip_continents = options["skip_continents"]
        self._skip_embeddings = options["skip_embeddings"]
        self._only_step = options["step"]
        self._no_lock = options["no_lock"]

        if not self._acquire_lock():
            return

        steps = list(self._all_steps())
        self.stdout.write(
            "init_planet: running {} step(s){}".format(
                len([s for s in steps if self._step_filter(s[0])]),
                f" (only: {self._only_step})" if self._only_step else "",
            )
        )

        failed: list[str] = []
        for name, fn in steps:
            if not self._step_filter(name):
                self.stdout.write(f"  [skip] {name}")
                continue
            self.stdout.write(self.style.NOTICE(f"  [run]  {name}"))
            try:
                fn()
                self.stdout.write(self.style.SUCCESS(f"  [ok]   {name}"))
            except Exception as exc:  # noqa: BLE001 — best-effort init chain
                failed.append(name)
                self.stderr.write(
                    self.style.ERROR(f"  [fail] {name}: {exc!r}")
                )
                logger.exception("init_planet step %s failed", name)

        if failed:
            self.stderr.write(
                self.style.WARNING(
                    f"init_planet: {len(failed)} step(s) failed: {', '.join(failed)}"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("init_planet: all steps complete"))
