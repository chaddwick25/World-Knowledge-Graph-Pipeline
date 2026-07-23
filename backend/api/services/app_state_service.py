"""
Application State Service.

Single source of truth for country pipeline state. Consolidates data
formerly spread across 5+ endpoints into one response.

Used by:
    GET /api/app-state/{country_name}/

Replaces ad-hoc queries to:
    - WorldKGPipelineCountryStateView   (ProcessingSession + PipelineRun)
    - CountrySearchStatusView            (CountrySearchProcessing)
    - WorldKGPipelineSummaryView         (SpatialTripletScore)
    - CountrySubgraphsView               (filesystem)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────


@dataclass
class SubgraphState:
    """Per-subgraph progress."""
    slug: str
    name: str
    status: str
    celery_state: Optional[str] = None
    metrics: Optional[dict] = None


@dataclass
class PipelineState:
    """Aggregated pipeline state."""
    status: Optional[str]
    run_id: Optional[str]
    pipeline_type: Optional[str]
    current_stage: Optional[str]
    completed_stages: list
    stage_metrics: dict
    queued_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    error_message: Optional[str]
    celery_state: Optional[str] = None
    is_zombie: Optional[bool] = None
    subgraphs: list = field(default_factory=list)


@dataclass
class SearchState:
    """Search readiness for a country."""
    is_ready: bool
    entity_count: int
    has_db_embeddings: bool


@dataclass
class CountryState:
    """Full response for GET /api/app-state/{country_name}/"""
    country_name: str
    iso: Optional[str]
    pipeline: Optional[PipelineState]
    search: SearchState
    capabilities: dict


# ── Step labels and ordering ──────────────────────────────────────────────


STEP_LABELS = {
    "embed_osm_entities": "Embed OSM Entities (GV-Tags)",
    "harvest_wikidata": "Harvest Wikidata Candidates",
    "run_igea": "IGEA Entity Alignment",
    "predict_spatial_links": "Predict Spatial Links",
    "train_gv_nle": "Train GV-NLE Embeddings",
    "mark_search_ready": "Mark Search Ready",
    "extract_region_pbf": "Extract Region PBF",
    "monthly_snapshots": "Generate Monthly Snapshots",
    "subgraph_generation": "Generate Subgraphs",
    "geovectors_preprocess": "GeoVectors Pre-processing",
}

STEPS_ORDER = {
    "worldkg_v2": [
        "embed_osm_entities",
        "harvest_wikidata",
        "run_igea",
        "predict_spatial_links",
        "train_gv_nle",
        "mark_search_ready",
    ],
    "temporal": [
        "extract_region_pbf",
        "monthly_snapshots",
        "subgraph_generation",
        "geovectors_preprocess",
    ],
}


# ── Service ────────────────────────────────────────────────────────────────


class AppStateService:
    """Builds the complete state for a country."""

    def get_state(self, country_name: str) -> CountryState:
        """Build full country state — called by the view."""
        iso = self._resolve_iso(country_name)
        pipeline = self._build_pipeline_state(country_name, iso)
        search = self._build_search_state(country_name, iso)
        capabilities = self._build_capabilities(pipeline, search)
        return CountryState(
            country_name=country_name,
            iso=iso,
            pipeline=pipeline,
            search=search,
            capabilities=capabilities,
        )

    # ── Pipeline State ────────────────────────────────────────────────

    def _build_pipeline_state(
        self, country_name: str, iso: Optional[str]
    ) -> Optional[PipelineState]:
        """Build pipeline state from PipelineRun + cross-check with Celery."""
        from orchestration.models import PipelineRun

        if not iso:
            return None

        run = PipelineRun.objects.filter(
            country_code=iso,
        ).order_by('-created_at').first()

        if not run:
            return None

        state = PipelineState(
            status=run.status,
            run_id=str(run.id),
            pipeline_type=run.pipeline_type,
            current_stage=run.current_stage,
            completed_stages=run.completed_stages or [],
            stage_metrics=run.stage_metrics or {},
            queued_at=run.queued_at.isoformat() if run.queued_at else None,
            started_at=run.started_at.isoformat() if run.started_at else None,
            completed_at=run.completed_at.isoformat() if run.completed_at else None,
            error_message=run.error_message,
        )

        if run.status == "RUNNING" and getattr(settings, 'CELERY_RESULT_BACKEND', None):
            state.celery_state, state.is_zombie = self._cross_check_celery(run)

        state.subgraphs = self._build_subgraph_states(run)
        return state

    @staticmethod
    def _cross_check_celery(run) -> tuple:
        """Cross-check PipelineRun status with Celery result backend."""
        try:
            from celery.result import AsyncResult
            from pipeline.celery_app import celery_app

            async_result = AsyncResult(str(run.id), app=celery_app)
            celery_state = async_result.state
            is_zombie = celery_state in ("SUCCESS", "FAILURE")
            return celery_state, is_zombie
        except Exception as exc:
            logger.debug(f"Celery cross-check failed: {exc}")
            return None, None

    @staticmethod
    def _build_subgraph_states(run) -> list:
        """Build per-subgraph states from stored task IDs."""
        task_ids = getattr(run, 'subgraph_task_ids', {}) or {}
        if not task_ids:
            return []

        states = []
        config = run.configuration or {}
        subgraph_configs = config.get("subgraphs", [])

        for sg_config in subgraph_configs:
            slug = sg_config.get("slug", "")
            task_id = task_ids.get(slug)

            sg_state = SubgraphState(
                slug=slug,
                name=sg_config.get("name", slug),
                status="pending",
            )

            if task_id:
                try:
                    from celery.result import AsyncResult
                    from pipeline.celery_app import celery_app

                    async_result = AsyncResult(task_id, app=celery_app)
                    celery_state = async_result.state
                    sg_state.celery_state = celery_state

                    if celery_state == "SUCCESS":
                        sg_state.status = "completed"
                        sg_state.metrics = async_result.result if async_result.result else None
                    elif celery_state == "FAILURE":
                        sg_state.status = "failed"
                    elif celery_state in ("RECEIVED", "STARTED"):
                        sg_state.status = "running"
                except Exception:
                    sg_state.celery_state = "unknown"

            states.append(sg_state)

        return states

    def get_pipeline_steps(self, pipeline_state: Optional[PipelineState]) -> list:
        """Build the ordered step list for the frontend."""
        if not pipeline_state or not pipeline_state.pipeline_type:
            step_names = STEPS_ORDER.get("worldkg_v2", [])
        else:
            step_names = STEPS_ORDER.get(pipeline_state.pipeline_type, [])

        completed = set(pipeline_state.completed_stages or []) if pipeline_state else set()
        current = pipeline_state.current_stage if pipeline_state else None
        is_complete = pipeline_state.status in ("COMPLETED", "FAILED") if pipeline_state else False

        steps = []
        for name in step_names:
            label = STEP_LABELS.get(name, name.replace("_", " ").title())

            if name in completed:
                status = "completed"
                message = "Completed"
                pct = 100
            elif name == current:
                status = "running"
                message = "Running\u2026"
                pct = 50
            elif is_complete:
                status = "skipped"
                message = "Skipped"
                pct = 0
            else:
                status = "pending"
                message = ""
                pct = 0

            steps.append({
                "name": name,
                "label": label,
                "status": status,
                "message": message,
                "pct": pct,
                "subgraphs": [],
            })

        # Attach subgraph states to relevant steps
        if pipeline_state and pipeline_state.subgraphs:
            subgraph_steps = {"predict_spatial_links", "embed_osm_entities", "train_gv_nle"}
            for step in steps:
                if step["name"] in subgraph_steps:
                    step["subgraphs"] = [
                        {
                            "slug": sg.slug,
                            "name": sg.name,
                            "status": sg.status,
                            "celery_state": sg.celery_state,
                            "metrics": sg.metrics,
                        }
                        for sg in pipeline_state.subgraphs
                    ]

        return steps

    # ── Search State ──────────────────────────────────────────────────

    @staticmethod
    def _build_search_state(country_name: str, iso: Optional[str]) -> SearchState:
        """Build search readiness state."""
        from orchestration.models import CountryPipelineProfile, CountrySearchProcessing

        profile = None
        if iso:
            profile = CountryPipelineProfile.objects.filter(iso2__iexact=iso).first()

        country_processing = CountrySearchProcessing.objects.filter(
            country_name__iexact=country_name
        ).first()
        is_ready = bool(country_processing and country_processing.is_processed)

        entity_count = 0
        has_db_embeddings = False
        if profile:
            from django.db import connections
            try:
                with connections['vectors'].cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) FROM osm_entity WHERE wkg_class IS NOT NULL"
                    )
                    entity_count = cursor.fetchone()[0]
                has_db_embeddings = entity_count > 0
            except Exception:
                pass

        return SearchState(
            is_ready=is_ready,
            entity_count=entity_count,
            has_db_embeddings=has_db_embeddings,
        )

    # ── Capabilities ──────────────────────────────────────────────────

    @staticmethod
    def _build_capabilities(pipeline: Optional[PipelineState], search: SearchState) -> dict:
        """Build frontend capability flags."""
        reasons_blocked = []
        can_run_pipeline = True
        can_show_search_button = search.is_ready
        can_show_subgraphs = False

        if pipeline and pipeline.status == "RUNNING":
            can_run_pipeline = False
            reasons_blocked.append("Pipeline already running")

        if pipeline and pipeline.is_zombie:
            can_run_pipeline = False
            reasons_blocked.append("Pipeline state is inconsistent (zombie)")

        return {
            "can_run_pipeline": can_run_pipeline,
            "can_show_search_button": can_show_search_button,
            "can_show_subgraphs": can_show_subgraphs,
            "reasons_blocked": reasons_blocked,
        }

    # ── Resolution Helpers ────────────────────────────────────────────

    @staticmethod
    def _resolve_iso(country_name: str) -> Optional[str]:
        """Resolve ISO code from country name."""
        from orchestration.models import CountryPipelineProfile

        profile = CountryPipelineProfile.objects.filter(
            canonical_name__iexact=country_name
        ).first()
        if profile and profile.iso2:
            return profile.iso2

        from worldkg_nca.services.pipeline_orchestrator import WorldKGPipelineService
        return WorldKGPipelineService._resolve_iso_code(country_name)
