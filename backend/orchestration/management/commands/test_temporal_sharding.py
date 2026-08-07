"""Test the temporal-sharding registry layer end-to-end.

The physical temporal sharding (a partitioned ``embeddings`` table + leaf
partitions + materialized merge views) is **planned, not built** — see
``docs/plans/TEMPORAL_SHARDING_ARTIFACT_PLAN.md`` §"Existing sharding". What
IS implemented today is the **registry layer** that tracks and queries those
partitions:

    - ``PartitionRegistry`` — completion gate per (snapshot, country, subdivision) leaf
    - ``PipelineAsset`` — artifact registry with continent denormalization (Phase 1)
    - ``ArtifactFacade`` / ``ArtifactService`` — frozen-dataclass query API (Phase 3)
    - REST endpoints — ``/api/artifacts/registry/``, ``/api/task-results/<run_id>/`` (Phase 3)

This command exercises every layer against the live DB using the existing
COMPLETED ``PipelineRun`` rows (CU/CV/IS/JM/PL) plus synthetic
``PartitionRegistry`` / ``PipelineAsset`` rows for a test snapshot. It creates
rows, queries them through each layer (ORM → facade → service → REST), prints
a structured report, then cleans up (unless ``--keep``).

Usage::

    python manage.py test_temporal_sharding            # run + cleanup
    python manage.py test_temporal_sharding --keep     # run, leave test rows
    python manage.py test_temporal_sharding --snapshot 2025_06_30

The test rows are tagged with ``metadata['__ts_test'] = True`` so cleanup is
precise and never touches real data.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client

from orchestration.models import (
    CountryPipelineProfile,
    PartitionRegistry,
    PipelineAsset,
    PipelineRun,
)
from orchestration.services.artifact_facade import ArtifactFacade
from orchestration.services.artifact_service import ArtifactService

TEST_TAG = "__ts_test"
DEFAULT_SNAPSHOT = "2025_12_31"


def _safe_json(response) -> Optional[dict]:
    """Parse JSON from a test-client response, returning None on failure."""
    try:
        return response.json()
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        "Test the temporal-sharding registry layer (PartitionRegistry + "
        "PipelineAsset + ArtifactFacade + REST) end-to-end against the live DB."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep", action="store_true",
            help="Keep the synthetic test rows after the run (default: clean up).",
        )
        parser.add_argument(
            "--snapshot", default=DEFAULT_SNAPSHOT,
            help=f"Snapshot id to use (default: {DEFAULT_SNAPSHOT}).",
        )

    def handle(self, *args, **options):
        keep = options["keep"]
        snapshot = options["snapshot"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Temporal sharding registry test (snapshot={snapshot}, keep={keep})"
        ))
        self.stdout.write("-" * 72)

        # ── 0. Pick a real COMPLETED run to attach assets to ──────────────
        run = self._pick_run()
        if run is None:
            self.stdout.write(self.style.ERROR(
                "No COMPLETED PipelineRun found — run a pipeline first."
            ))
            return
        country_code = run.country_code
        continent = self._resolve_continent(country_code)
        self.stdout.write(self.style.SUCCESS(
            f"[0] Using PipelineRun: {country_code} ({run.country_name}) "
            f"continent={continent!r} run_id={run.id}"
        ))

        created_asset_ids: List[str] = []
        created_partition_keys: List[Tuple[str, str, Optional[str]]] = []

        try:
            with transaction.atomic():
                # ── 1. PartitionRegistry: create leaves (the partition key) ──
                self._test_partition_registry(snapshot, country_code, created_partition_keys)

                # ── 2. PipelineAsset: create artifacts linked to the run ─────
                self._test_pipeline_assets(run, snapshot, continent, created_asset_ids)

            # ── 3. Query: ArtifactFacade (frozen dataclasses, shard-routed) ──
            self._test_facade(country_code, snapshot)

            # ── 4. Query: ArtifactService (REST-facing JSON) ─────────────────
            self._test_service(country_code, run.id)

            # ── 5. Query: REST endpoints (end-to-end via test client) ────────
            self._test_rest(country_code, run.id)

            self.stdout.write("-" * 72)
            self.stdout.write(self.style.SUCCESS(
                "All layers passed. "
                f"Created {len(created_partition_keys)} partition rows, "
                f"{len(created_asset_ids)} asset rows."
            ))

        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Test failed: {exc}"))
            raise
        finally:
            if not keep:
                self._cleanup(created_asset_ids, created_partition_keys)
                self.stdout.write(self.style.WARNING(
                    "Cleaned up synthetic test rows (use --keep to preserve)."
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"Kept {len(created_asset_ids)} asset + "
                    f"{len(created_partition_keys)} partition test rows "
                    f"(tagged metadata['{TEST_TAG}']=True)."
                ))

    # ── Layer 1: PartitionRegistry (the leaf completion gate) ─────────────

    def _test_partition_registry(
        self, snapshot: str, country_code: str,
        created: List[Tuple[str, str, Optional[str]]],
    ) -> None:
        """Create leaves for (Belize single-leaf) + (Mozambique multi-leaf).

        This demonstrates the two leaf shapes from the plan:
          - Small country (BZ): one leaf, subdivision=NULL
          - Big country (MZ): multiple leaves — one per subdivision + one
            default (NULL) leaf for entities with no subdivision
        We also create a leaf for the test's own country (the run's country)
        so the registry has a real completion record to query.
        """
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[1] PartitionRegistry — creating leaves (the partition key)"
        ))
        leaves = [
            # (snapshot, country, subdivision, entity_count, embedding_count)
            (snapshot, "BZ", None, 4_200, 4_200),               # Belize, single leaf
            (snapshot, "MZ", None, 18_000, 18_000),             # Mozambique default leaf
            (snapshot, "MZ", "cabo_delgado", 3_100, 3_100),     # MZ subdivision leaf
            (snapshot, "MZ", "nampula", 2_400, 2_400),          # MZ subdivision leaf
            (snapshot, country_code, None, 9_500, 9_500),       # the run's own country
        ]
        for snap, cc, sub, ent, emb in leaves:
            obj, created_now = PartitionRegistry.get_or_create_pending(snap, cc, sub)
            if created_now or obj.status != PartitionRegistry.PartitionStatus.COMPLETE:
                obj.mark_complete(entity_count=ent, embedding_count=emb)
            created.append((snap, cc, sub))
            label = f"{cc}/{sub}" if sub else f"{cc}/<default>"
            self.stdout.write(
                f"    leaf {label}: status={obj.status} "
                f"entities={obj.entity_count} embeddings={obj.embedding_count}"
            )

        # Query: idempotency gate (the core PartitionRegistry use case)
        is_bz_done = PartitionRegistry.is_complete(snapshot, "BZ")
        is_mz_cabo_done = PartitionRegistry.is_complete(snapshot, "MZ", "cabo_delgado")
        is_mz_nampula_done = PartitionRegistry.is_complete(snapshot, "MZ", "nampula")
        self.stdout.write(
            f"    is_complete(BZ)={is_bz_done}  "
            f"is_complete(MZ/cabo_delgado)={is_mz_cabo_done}  "
            f"is_complete(MZ/nampula)={is_mz_nampula_done}"
        )
        # MZ country-level completeness = ALL subdivision leaves complete.
        mz_leaves = PartitionRegistry.objects.filter(
            snapshot_id=snapshot, country_code="MZ",
        )
        mz_all_complete = all(
            l.status == PartitionRegistry.PartitionStatus.COMPLETE for l in mz_leaves
        )
        self.stdout.write(
            f"    MZ country-level complete (all leaves) = {mz_all_complete}  "
            f"[{mz_leaves.count()} leaf rows]"
        )

    # ── Layer 2: PipelineAsset (the artifact registry) ─────────────────────

    def _test_pipeline_assets(
        self, run: PipelineRun, snapshot: str, continent: str,
        created: List[str],
    ) -> None:
        """Create PipelineAsset rows linked to the run, tagged for cleanup."""
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[2] PipelineAsset — creating artifacts linked to the run"
        ))
        assets = [
            {
                "asset_type": PipelineAsset.AssetType.GV_TAGS_EMBEDDING,
                "asset_name": f"{run.country_code}_gv_tags_{snapshot}",
                "stage_name": "embed_osm_entities",
                "storage_path": f"/app/data/embeddings/{run.country_code.lower()}/tags.tsv.gz",
                "record_count": 9_500,
                "metadata": {"snapshot_id": snapshot, "subdivision": None, TEST_TAG: True},
            },
            {
                "asset_type": PipelineAsset.AssetType.GV_NLE_EMBEDDING,
                "asset_name": f"{run.country_code}_gv_nle_{snapshot}",
                "stage_name": "train_gv_nle",
                "storage_path": f"/app/data/embeddings/{run.country_code.lower()}/nle.npy",
                "record_count": 9_500,
                "metadata": {"snapshot_id": snapshot, "subdivision": None, TEST_TAG: True},
            },
            {
                "asset_type": PipelineAsset.AssetType.SUBGRAPH_PICKLE,
                "asset_name": f"{run.country_code}_subgraph_{snapshot}_warsaw",
                "stage_name": "train_gv_nle",
                "storage_path": f"/app/data/embeddings/{run.country_code.lower()}/wdw.pickle",
                "record_count": 1,
                "metadata": {"snapshot_id": snapshot, "subdivision": "warsaw", TEST_TAG: True},
            },
        ]
        for desc in assets:
            asset = PipelineAsset(
                pipeline_run=run,
                asset_type=desc["asset_type"],
                asset_name=desc["asset_name"],
                stage_name=desc["stage_name"],
                storage_path=desc["storage_path"],
                record_count=desc["record_count"],
                continent=continent or None,
                status=PipelineAsset.AssetStatus.COMPLETED,
                metadata=desc["metadata"],
            )
            asset.save()
            created.append(str(asset.id))
            self.stdout.write(
                f"    asset {asset.id}: {asset.asset_type} stage={asset.stage_name} "
                f"continent={asset.continent!r} subdivision={desc['metadata']['subdivision']!r}"
            )

    # ── Layer 3: ArtifactFacade (frozen dataclasses, shard-routed) ─────────

    def _test_facade(self, country_code: str, snapshot: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[3] ArtifactFacade.for_country({country_code!r}) — frozen handles"
        ))
        facade = ArtifactFacade.for_country(country_code)
        self.stdout.write(f"    resolved continent={facade.continent!r} shard={facade.shard!r}")

        # get_artifact_handle: most-recent-completed of a type
        handle = facade.get_artifact_handle("GV_TAGS_EMBEDDING")
        self.stdout.write(
            f"    get_artifact_handle(GV_TAGS_EMBEDDING) -> "
            f"{handle.artifact_id if handle else None} "
            f"records={handle.record_count if handle else 'n/a'}"
        )

        # list_artifacts: all assets for the country
        all_handles = facade.list_artifacts()
        self.stdout.write(f"    list_artifacts() -> {len(all_handles)} handles")
        for h in all_handles:
            self.stdout.write(
                f"      - {h.asset_type} stage={h.stage_name} "
                f"snap={h.metadata.get('snapshot_id')} sub={h.metadata.get('subdivision')!r}"
            )

        # resolve_artifacts: the sharding-key lookup (snapshot, country, subdivision, step, type)
        scoped = facade.resolve_artifacts(snapshot=snapshot, asset_type="GV_TAGS_EMBEDDING")
        self.stdout.write(
            f"    resolve_artifacts(snapshot={snapshot!r}, type=GV_TAGS_EMBEDDING) "
            f"-> {len(scoped)} handles"
        )
        sub_handles = facade.resolve_artifacts(snapshot=snapshot, subdivision="warsaw")
        self.stdout.write(
            f"    resolve_artifacts(snapshot={snapshot!r}, subdivision='warsaw') "
            f"-> {len(sub_handles)} handles"
        )

    # ── Layer 4: ArtifactService (REST-facing JSON) ────────────────────────

    def _test_service(self, country_code: str, run_id) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[4] ArtifactService — REST-facing JSON"
        ))
        service = ArtifactService()
        rows = service.list_artifacts(country_code=country_code)
        self.stdout.write(f"    list_artifacts({country_code!r}) -> {len(rows)} dicts")
        avail = service.availability_by_stage(country_code=country_code)
        self.stdout.write(f"    availability_by_stage -> {len(avail)} stage rows:")
        for a in avail:
            self.stdout.write(
                f"      - stage={a['stage_name']} type={a['asset_type']} "
                f"status={a['status']} count={a['count']} records={a['record_count_sum']}"
            )
        single = service.get_artifact(rows[0]["artifact_id"]) if rows else None
        self.stdout.write(
            f"    get_artifact({rows[0]['artifact_id'][:8]}...) -> "
            f"{'OK' if single else 'None'}"
        )

    # ── Layer 5: REST endpoints (end-to-end via test client) ───────────────

    def _test_rest(self, country_code: str, run_id) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[5] REST endpoints — Django test client"
        ))
        # Pass HTTP_HOST explicitly so the request passes ALLOWED_HOSTS
        # (['localhost', '127.0.0.1']); the default 'testserver' is rejected.
        client = Client()

        # /api/artifacts/registry/?country_code=...
        r = client.get(
            "/api/artifacts/registry/", {"country_code": country_code},
            HTTP_HOST="localhost",
        )
        body = _safe_json(r)
        self.stdout.write(
            f"    GET /api/artifacts/registry/?country_code={country_code} "
            f"-> {r.status_code} count={body.get('count') if body else 'n/a'}"
        )

        # /api/artifacts/registry/by-stage/?country_code=...
        r = client.get(
            "/api/artifacts/registry/by-stage/", {"country_code": country_code},
            HTTP_HOST="localhost",
        )
        body = _safe_json(r)
        self.stdout.write(
            f"    GET /api/artifacts/registry/by-stage/?country_code={country_code} "
            f"-> {r.status_code} availability={body.get('count') if body else 'n/a'}"
        )

        # /api/task-results/<run_id>/
        r = client.get(f"/api/task-results/{run_id}/", HTTP_HOST="localhost")
        body = _safe_json(r)
        run_status = (body or {}).get("run", {}).get("status")
        self.stdout.write(
            f"    GET /api/task-results/{run_id}/ -> {r.status_code} "
            f"run_status={run_status} "
            f"tasks={len((body or {}).get('tasks', []))} "
            f"artifacts_by_task={len((body or {}).get('artifacts_by_task', {}))}"
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_run() -> Optional[PipelineRun]:
        return (
            PipelineRun.objects.filter(status=PipelineRun.PipelineStatus.COMPLETED)
            .order_by("-completed_at").first()
        )

    @staticmethod
    def _resolve_continent(country_code: str) -> str:
        profile = (
            CountryPipelineProfile.objects.filter(iso2=country_code).first()
            or CountryPipelineProfile.objects.filter(iso3=country_code).first()
        )
        if profile is None:
            return ""
        payload = profile.country_relations_payload or {}
        return payload.get("continent") or profile.continent_name or ""

    def _cleanup(
        self,
        asset_ids: List[str],
        partition_keys: List[Tuple[str, str, Optional[str]]],
    ) -> None:
        # Wipe the test-tagged assets (belt + suspenders: also by ID).
        PipelineAsset.objects.filter(id__in=asset_ids).delete()
        PipelineAsset.objects.filter(metadata__contains={TEST_TAG: True}).delete()
        for snap, cc, sub in partition_keys:
            PartitionRegistry.objects.filter(
                snapshot_id=snap, country_code=cc, subdivision=sub,
            ).delete()
