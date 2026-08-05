"""Helper functions for WorldKG pipeline tasks."""

from __future__ import annotations
import logging
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING, Union
from django.core.management import call_command
from django.conf import settings
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope, PlanetEnvelope

# Type alias — helper functions accept either envelope (duck-typed).
CfgLike = Union["CountryEnvelope", "PlanetEnvelope"]


# Configuration Driven Development
# Functions as first class objects with argument keyword capture via **kwargs
# _log: consistent logging dispatcher for pipeline steps
def _log(logger: logging.Logger, level: str, message: str, **kwargs) -> None:
    """Internal logging helper to mirror the v1 formatting.
    Formats messages as ``"msg [k1=v1 k2=v2]"`` when extra context is given.
    """
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    getattr(logger, level)(f"{message} [{extra}]" if extra else message)


# ---------------------------------------------------------------------------
# WebSocket Update
# ---------------------------------------------------------------------------
def _push_update(pipeline_run_id: str, name: str, status: str, message: str,
                 pct: int = 0, step: int = 0, total: int = 6) -> None:
    """Send a step update via WebSocket.
    Args:
        pipeline_run_id: UUID of the pipeline run.
        name: Canonical step name (must match app_state_service STEP_NAMES).
        status: 'in_progress', 'completed', 'failed', or 'skipped'.
        message: Human-readable status message.
        pct: Percentage complete for this step (0-100).
        step: Step index (0-based, deprecated — use name for matching).
        total: Total number of steps in this pipeline (default 6 for WorldKG v2).
    """
    try:
        # private logger for this function :)
        _logger = logging.getLogger("pipeline")
        channel_layer = get_channel_layer()
        _logger.info(
            f"[_push_update] name={name} status={status} pct={pct} "
        )
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f"pipeline_{pipeline_run_id}",
                {
                    "type": "step_update",
                    "name": name,
                    "status": status,
                    "message": message,
                    "pct": pct,
                    "step": step,
                    "total": total,
                },
            )
    except Exception as exc:
        _logger.debug(f"Failed to push update: {exc}")


# ---------------------------------------------------------------------------
# WorldKG Enrichment
# ---------------------------------------------------------------------------
def enrich_worldkg_classes(cfg: CfgLike, logger: logging.Logger) -> None:
    """Populate wkg_class via Redis ontology matching """
    # Guard: skip enrichment if requested
    if getattr(cfg, "skip_enrich", False):
        _log(
            logger,
            "info",
            "Skipping WorldKG enrichment (skip_enrich=True)",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return

    _log(
        logger,
        "info",
        "Enriching WorldKG classes",
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
    )
    # TODO: remove the if statement, the validation logic should be done before calling this function
    poly_path = cfg.poly_path
    if not poly_path:
        if cfg.snapshot_pbf_path:
            snap_poly = Path(cfg.snapshot_pbf_path).with_suffix(".poly")
            if snap_poly.exists():
                poly_path = str(snap_poly)
        if not poly_path:
            cont_norm = cfg.continent.replace("-", "_")
            candidate = (
                Path(settings.BASE_DATA_DIR)
                / "OSM-PBF-FILES"
                / "osm_polygon_files"
                / cont_norm
                / f"{cfg.slug}.poly"
            )
            if candidate.exists():
                poly_path = str(candidate)

    from semantic_search.services.worldkg_enrichment_service import (  # type: ignore
        get_worldkg_enrichment_service,
    )

    svc = get_worldkg_enrichment_service()
    svc.batch_enrich_region(
        region=cfg.iso,
        poly_file=poly_path,
    )


# ---------------------------------------------------------------------------
# Entropy Computation (v2)
# ---------------------------------------------------------------------------
# TODO: Review the math for this function
def compute_entropy(cfg: CfgLike, logger: logging.Logger) -> float:
    """Compute Shannon entropy of WorldKG class distribution (v2 helper)."""
    import numpy as np
    from semantic_search.services.worldkg_enrichment_service import (
        get_worldkg_enrichment_service,
    )

    svc = get_worldkg_enrichment_service()
    distribution = svc.get_class_distribution(snapshot_id=None, region=cfg.iso)
    if not distribution:
        _log(
            logger,
            "warning",
            "No WorldKG classes found -- entropy = 0",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return 0.0

    total = float(sum(distribution.values()))
    if total <= 0:
        _log(
            logger,
            "warning",
            "WorldKG class distribution empty or zero-count -- entropy = 0",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return 0.0

    probs = np.array([count / total for count in distribution.values()], dtype=float)
    probs = probs[probs > 0]
    entropy = -float(np.sum(probs * np.log(probs)))

    top5 = sorted(distribution.items(), key=lambda x: x[1], reverse=True)[:5]
    top5_str = ", ".join(f"{cls}={count}" for cls, count in top5)
    _log(
        logger,
        "info",
        "Entropy computed",
        entropy=round(entropy, 4),
        total_classes=len(distribution),
        total_entities=int(total),
        top5_classes=top5_str,
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
    )
    return entropy


# ---------------------------------------------------------------------------
# GV-NLE Training (v2)
# ---------------------------------------------------------------------------

# ══════════════════════════════════════════════════════════════════════════
# Planet Record
# ══════════════════════════════════════════════════════════════════════════
def create_planet_run_record(cfg: CfgLike) -> None:
    """Create or update a PlanetSnapshot record for tracking."""
    from orchestration.models import PlanetSnapshot
    from django.utils import timezone as tz

    snapshot_date = datetime.now().date()

    PlanetSnapshot.objects.get_or_create(
        snapshot_date=snapshot_date,
        planet_osm_path=cfg.pbf_path or settings.PLANET_OSM_FILE_PATH,
        defaults={
            "status": PlanetSnapshot.SnapshotStatus.COMPLETED,
            "completed_at": tz.now(),
        },
    )


def preprocess_snapshot(cfg: CfgLike, logger: logging.Logger) -> None:
    """Generate snapshot PBF + poly file if they don't exist (v2 helper)."""
    from extraction.services.temporal_orchestrator_service import (
        TemporalOrchestratorService,
    )
    from extraction.services.regional_path_service import (
        regional_path_service,
        normalize_continent_slug,
        normalize_country_slug,
    )
    from extraction.services.country_override_service import (
        get_country_slug as get_override_country_slug,
    )

    snapshot_exists = cfg.snapshot_pbf_path and Path(cfg.snapshot_pbf_path).exists()

    if not snapshot_exists:
        if not cfg.osm_relation_id:
            _log(
                logger,
                "warning",
                "No OSM relation ID for this country — cannot pre-process",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            return

        _log(
            logger,
            "info",
            "Pre-processing snapshot via TemporalOrchestratorService (phases 1-3)",
            country=cfg.iso,
            continent=cfg.continent,
            osm_relation_id=cfg.osm_relation_id,
            snapshot_date=cfg.snapshot_date,
            pipeline_run_id=cfg.pipeline_run_id,
        )

        orchestrator = TemporalOrchestratorService()
        result = orchestrator.run_pipeline(
            continent=cfg.continent,
            country=cfg.name,
            source_pbf_path=settings.PLANET_OSM_FILE_PATH,
            start_year=int(cfg.snapshot_date.split("_")[0]) if cfg.snapshot_date else 2025,
            end_year=int(cfg.snapshot_date.split("_")[0]) if cfg.snapshot_date else 2025,
            phases=[1, 2, 3],
            single_snapshot_mode=True,
            snapshot_date=cfg.snapshot_date,
        )

        if not result.get("success"):
            err_msg = result.get("error", "")
            if "not found in mapping" in err_msg:
                _log(
                    logger,
                    "info",
                    "Country not in OSMWikiDataHierarchy — using direct relation extraction",
                    country=cfg.iso,
                    osm_relation_id=cfg.osm_relation_id,
                    pipeline_run_id=cfg.pipeline_run_id,
                )
                result = _preprocess_synthetic_territory(cfg, logger)
            else:
                _log(
                    logger,
                    "error",
                    "Pre-processing failed",
                    country=cfg.iso,
                    error=err_msg,
                    pipeline_run_id=cfg.pipeline_run_id,
                )
                raise RuntimeError(
                    f"Pre-processing failed for {cfg.iso}: {err_msg}"
                )

        if not result.get("success"):
            _log(
                logger,
                "error",
                "Pre-processing failed",
                country=cfg.iso,
                error=result.get("error", "Unknown error"),
                pipeline_run_id=cfg.pipeline_run_id,
            )
            raise RuntimeError(
                f"Pre-processing failed for {cfg.iso}: {result.get('error')}"
            )

        _log(
            logger,
            "info",
            "Pre-processing complete",
            country=cfg.iso,
            phases=result.get("phases", {}),
            pipeline_run_id=cfg.pipeline_run_id,
        )

        try:
            cont_norm = normalize_continent_slug(cfg.continent)
            default_slug = normalize_country_slug(cfg.name)
            snapshot_slug = (
                get_override_country_slug(cfg.iso, default_slug)
                if cfg.iso
                else default_slug
            )
            ss_path = regional_path_service.get_single_snapshot_pbf_path(
                cont_norm,
                snapshot_slug,
                cfg.snapshot_date,
            )
            cfg.snapshot_pbf_path = str(ss_path)
            snapshot_exists = ss_path.exists()
        except Exception:
            snapshot_exists = cfg.snapshot_pbf_path and Path(cfg.snapshot_pbf_path).exists()
    else:
        _log(
            logger,
            "info",
            "Snapshot PBF already exists",
            path=cfg.snapshot_pbf_path,
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )

    if cfg.osm_relation_id and snapshot_exists:
        try:
            from extraction.services.subgraph_pbf_service import subgraph_pbf_service

            country_slug_for_subgraphs = (
                get_override_country_slug(
                    cfg.iso,
                    cfg.slug or normalize_country_slug(cfg.name),
                )
                if cfg.iso
                else (cfg.slug or normalize_country_slug(cfg.name))
            )

            subgraph_result = subgraph_pbf_service.generate_subgraph_pbfs(
                continent=normalize_continent_slug(cfg.continent),
                country=country_slug_for_subgraphs,
                iso=cfg.iso,
                snapshot_date=cfg.snapshot_date,
                relation_id=cfg.osm_relation_id,
                country_name=cfg.name,
                overwrite=False,
            )
            if subgraph_result.get("generated", 0) > 0 or subgraph_result.get("failed", 0) > 0:
                sync_subgraph_profiles(
                    iso=cfg.iso,
                    continent=cfg.continent,
                    country_slug=cfg.slug,
                    subgraph_results=subgraph_result.get("subgraphs", []),
                    logger=logger,
                )
                _log(
                    logger,
                    "info",
                    "Subgraph generation complete",
                    country=cfg.iso,
                    generated=subgraph_result.get("generated", 0),
                    skipped=subgraph_result.get("skipped", 0),
                    total=subgraph_result.get("total", 0),
                    pipeline_run_id=cfg.pipeline_run_id,
                )
            elif subgraph_result.get("total", 0) == 0:
                _log(
                    logger,
                    "info",
                    "No subgraphs found in hierarchy for this country",
                    country=cfg.iso,
                    pipeline_run_id=cfg.pipeline_run_id,
                )
        except Exception as exc:
            _log(
                logger,
                "warning",
                "Subgraph generation failed (non-fatal, continuing with country-level)",
                country=cfg.iso,
                error=str(exc),
                pipeline_run_id=cfg.pipeline_run_id,
            )


def _preprocess_synthetic_territory(
    cfg: CfgLike,
    logger: logging.Logger,
) -> dict:
    """Direct extraction pipeline for synthetic non-sovereign territories (v2)."""
    from extraction.services.regional_path_service import (
        regional_path_service,
        normalize_continent_slug,
        normalize_country_slug,
    )
    from extraction.services.extraction_service import (
        run_pbf_extraction_with_relation,
        ExtractionService,
    )
    from extraction.services.pbf_bounding_box_service import pbf_bounding_box_service
    from api.models import PbfFile

    cont_norm = normalize_continent_slug(cfg.continent)
    country_slug = normalize_country_slug(cfg.name)
    snapshot_date = cfg.snapshot_date
    year = int(snapshot_date.split("_")[0]) if snapshot_date else 2025

    osm_wikidata_dir = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR)
    possible_sources = [
        regional_path_service.get_continent_snapshot_pbf_path(cont_norm, snapshot_date),
        osm_wikidata_dir / "continents" / f"{cont_norm}.pbf",
        osm_wikidata_dir / cont_norm / f"{cont_norm}.pbf",
        Path(settings.PLANET_OSM_FILE_PATH),
    ]

    effective_source = None
    for ps in possible_sources:
        if ps.exists():
            effective_source = str(ps)
            break

    if not effective_source:
        return {"success": False, "error": "No continent source PBF found"}

    rel_id = cfg.osm_relation_id
    if not rel_id:
        return {"success": False, "error": "No OSM relation ID for synthetic territory"}

    generated = 0
    failed = 0
    shared_used_cores = set()

    yearly_pbf_path = regional_path_service.get_yearly_pbf_path(cont_norm, country_slug, year)
    if not yearly_pbf_path.exists():
        _log(
            logger,
            "info",
            "Phase 1: Extracting synthetic territory PBF via relation",
            country=cfg.iso,
            relation_id=rel_id,
            source=effective_source,
            output=str(yearly_pbf_path),
            pipeline_run_id=cfg.pipeline_run_id,
        )
        res = run_pbf_extraction_with_relation(
            effective_source,
            rel_id,
            str(yearly_pbf_path),
            shared_used_cores,
            f"syn_{cfg.iso}_{year}",
        )
        if res.get("success"):
            generated += 1
        else:
            failed += 1
            return {
                "success": False,
                "error": f"Phase 1 failed: {res.get('error', 'Unknown error')}",
            }
    else:
        _log(
            logger,
            "info",
            "Phase 1: Yearly PBF already exists",
            path=str(yearly_pbf_path),
            pipeline_run_id=cfg.pipeline_run_id,
        )

    snapshot_path = regional_path_service.get_single_snapshot_pbf_path(
        cont_norm,
        country_slug,
        snapshot_date,
    )
    if not snapshot_path.exists():
        syear, smonth, sday = map(int, snapshot_date.split("_"))
        snapshot_timestamp = f"{syear}-{smonth:02d}-{sday:02d}T23:59:59Z"
        _log(
            logger,
            "info",
            "Phase 2: Creating single snapshot",
            country=cfg.iso,
            source=str(yearly_pbf_path),
            output=str(snapshot_path),
            timestamp=snapshot_timestamp,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        extraction_service = ExtractionService()
        res = extraction_service.create_snapshot(
            str(yearly_pbf_path),
            str(snapshot_path),
            snapshot_timestamp,
            PbfFile.ExtractionLevel.SNAPSHOT,
        )
        if not res.get("success"):
            failed += 1
            return {
                "success": False,
                "error": f"Phase 2 failed: {res.get('error', 'Unknown error')}",
            }
        generated += 1
    else:
        _log(
            logger,
            "info",
            "Phase 2: Snapshot already exists",
            path=str(snapshot_path),
            pipeline_run_id=cfg.pipeline_run_id,
        )

    pbf_name = snapshot_path.name
    poly_name = pbf_name.replace(".pbf", ".poly", 1)
    poly_path = snapshot_path.with_name(poly_name)
    if not poly_path.exists():
        _log(
            logger,
            "info",
            "Phase 3: Generating poly boundary file",
            country=cfg.iso,
            snapshot=str(snapshot_path),
            relation_id=rel_id,
            output=str(poly_path),
            pipeline_run_id=cfg.pipeline_run_id,
        )
        success = pbf_bounding_box_service.generate_high_res_poly(
            str(snapshot_path),
            rel_id,
            str(poly_path),
        )
        if success:
            generated += 1
        else:
            failed += 1
            return {"success": False, "error": "Phase 3: poly generation failed"}
    else:
        _log(
            logger,
            "info",
            "Phase 3: Poly already exists",
            path=str(poly_path),
            pipeline_run_id=cfg.pipeline_run_id,
        )

    cfg.snapshot_pbf_path = str(snapshot_path)
    cfg.poly_path = str(poly_path)

    return {
        "success": failed == 0,
        "phases": {
            "1": {"success": True},
            "2": {"success": True},
            "3": {"success": True},
        },
    }


def sync_subgraph_profiles(
    iso: str,
    continent: str,
    country_slug: str,
    subgraph_results: list,
    logger: logging.Logger,
) -> None:
    """Sync SubgraphProfile DB records from subgraph generation results (v2)."""

    from orchestration.models import CountryPipelineProfile, SubgraphProfile
    profile = CountryPipelineProfile.objects.filter(iso2__iexact=iso).first()
    if not profile:
        _log(
            logger,
            "warning",
            "Cannot sync subgraph profiles — no CountryPipelineProfile found",
            country=iso,
        )
        return

    for sg in subgraph_results:
        sg_name = sg.get("name", "")
        sg_slug = sg.get("slug", "")
        sg_pbf = sg.get("pbf_path", "")
        sg_poly = sg.get("poly_path", "")
        sg_pickle = sg.get("pickle_path", "")

        SubgraphProfile.objects.update_or_create(
            country_profile=profile,
            slug=sg_slug,
            defaults={
                "name": sg_name,
                "has_subgraph_pbf": bool(sg_pbf),
                "subgraph_pbf_path": sg_pbf or None,
                "subgraph_poly_path": sg_poly or None,
                "subgraph_pickle_path": sg_pickle or None,
            },
        )

    _log(
        logger,
        "info",
        f"Synced {len(subgraph_results)} subgraph profiles",
        country=iso,
    )