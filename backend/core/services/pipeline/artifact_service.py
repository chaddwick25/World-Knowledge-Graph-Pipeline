"""ArtifactService — thin REST-facing wrapper over ArtifactFacade.

Phase 3 of ``docs/plans/TEMPORAL_SHARDING_ARTIFACT_PLAN.md``:

    Add ``ArtifactService`` with ``resolve_artifacts(snapshot, country,
    subdivision, step=None)`` as a thin wrapper over the facade (for REST
    endpoints that need JSON, not frozen dataclasses).

The facade returns frozen dataclasses (``ArtifactHandle`` / ``EmbeddingSlice``)
which are great for operators but awkward for DRF serializers. This service
turns them into plain dicts (``handle.to_dict()``) and adds a couple of
query conveniences (filter by run, by country, latest-by-type) that REST
endpoints need but operators don't.

It does NOT replace the facade for operator use — operators should still
construct an ``ArtifactFacade`` directly from their envelope. This service is
for the API/MCP-server layer.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from django.db.models import Count, Max, Sum
from core.services.pipeline.artifact_facade import (
    ArtifactFacade,
    ArtifactHandle,
    continent_to_shard_alias,
)
logger = logging.getLogger(__name__)


class ArtifactService:
    """REST/MCP-facing artifact queries.

    Stateless — construct per-request. All methods return JSON-serializable
    dicts (or lists of dicts), never ORM objects or frozen dataclasses.
    """

    # ── List / resolve ───────────────────────────────────────────────────

    def list_artifacts(
        self,
        country_code: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
        asset_type: Optional[str] = None,
        stage_name: Optional[str] = None,
        status: Optional[str] = None,
        snapshot: Optional[str] = None,
        subdivision: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List artifacts, optionally filtered by country, run, type, stage, status.

        When ``country_code`` is given, the query is shard-routed via the
        facade. When only ``pipeline_run_id`` is given, the facade is built
        from the run (which resolves the country and shard).
        """
        facade = self._facade(country_code=country_code,
                              pipeline_run_id=pipeline_run_id)
        if facade is None:
            return []
        if snapshot or subdivision:
            handles = facade.resolve_artifacts(
                snapshot=snapshot, subdivision=subdivision,
                step=stage_name, asset_type=asset_type,
            )
        else:
            handles = facade.list_artifacts(
                asset_type=asset_type, stage_name=stage_name,
                status=status, limit=limit,
            )
        if limit is not None:
            handles = handles[: int(limit)]
        return [h.to_dict() for h in handles]

    def resolve_artifacts(
        self,
        country_code: str,
        snapshot: Optional[str] = None,
        subdivision: Optional[str] = None,
        step: Optional[str] = None,
        asset_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve artifacts for a (snapshot, country, subdivision, step, type) scope.

        Implements the sharding-key lookup from the plan. Thin wrapper over
        ``ArtifactFacade.resolve_artifacts`` returning JSON dicts.
        """
        facade = ArtifactFacade.for_country(country_code)
        handles = facade.resolve_artifacts(
            snapshot=snapshot, subdivision=subdivision,
            step=step, asset_type=asset_type,
        )
        return [h.to_dict() for h in handles]

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single artifact by ID.

        Without a country code we cannot resolve a shard; today (pre-sharding)
        everything lives on ``default`` so a direct lookup is safe. Post-sharding
        this will need a continent hint or a control-DB index of asset_id -> shard.
        """
        from core.models import PipelineAsset
        asset = PipelineAsset.objects.filter(id=artifact_id).first()
        if asset is None:
            return None
        country_code = asset.pipeline_run.country_code if asset.pipeline_run_id else ""
        continent = asset.continent or ArtifactFacade._resolve_continent(country_code)
        handle = ArtifactHandle.from_asset(
            asset, continent=continent, country_code=country_code,
        )
        return handle.to_dict()

    # ── Aggregate conveniences (for the Artifacts tab) ───────────────────

    def availability_by_stage(
        self,
        country_code: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Group artifacts by stage_name with status + counts — for the frontend Artifacts tab.

        Returns one row per ``(stage_name, asset_type, status)`` triple with
        ``count``, ``record_count_sum``, and ``latest_completed_at``. This is
        the "data availability view" from the plan.
        """
        from core.models import PipelineAsset
        facade = self._facade(country_code=country_code,
                              pipeline_run_id=pipeline_run_id)
        if facade is None:
            return []
        qs = PipelineAsset.objects.using(facade.shard).filter(
            pipeline_run__country_code=facade._country_code,
        ) if facade._country_code else PipelineAsset.objects.using(facade.shard).all()
        if pipeline_run_id:
            qs = qs.filter(pipeline_run_id=pipeline_run_id)
        rows = qs.values("stage_name", "asset_type", "status").annotate(
            count=Count("id"),
            record_count_sum=Sum("record_count"),
            latest_completed_at=Max("completed_at"),
        ).order_by("stage_name", "asset_type")
        return [
            {
                "stage_name": r["stage_name"],
                "asset_type": r["asset_type"],
                "status": r["status"],
                "count": r["count"],
                "record_count_sum": r["record_count_sum"] or 0,
                "latest_completed_at": r["latest_completed_at"].isoformat() if r["latest_completed_at"] else None,
            }
            for r in rows
        ]

    # ── Internals ────────────────────────────────────────────────────────

    @staticmethod
    def _facade(country_code: Optional[str] = None,
                pipeline_run_id: Optional[str] = None) -> Optional[ArtifactFacade]:
        """Build a facade from a country code or a run id.

        Returns ``None`` when neither is provided (caller should return an
        empty list — there is nothing to scope the query).
        """
        if country_code:
            return ArtifactFacade.for_country(country_code)
        if pipeline_run_id:
            try:
                return ArtifactFacade.for_run(pipeline_run_id)
            except ValueError as exc:
                logger.warning("ArtifactService: %s", exc)
                return None
        return None
