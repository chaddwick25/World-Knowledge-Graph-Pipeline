"""Celery tasks for USLP (spatial link prediction) at subgraph level.

These tasks wrap the existing SpatialLinkPredictionService so that
link prediction can be executed asynchronously for one or more
subgraphs (defined by their .poly boundary files).

The tasks are designed to:
- Scope head entities by polygon (per subgraph)
- Persist accepted links to the database via SpatialTripletScore
- Write an optional JSONL file with accepted links per subgraph
- Record per-subgraph metrics in ProcessingSession.results (if provided)
- Emit websocket step_update events compatible with the existing
  CountryPipelineSchematic / WorldKG pipeline frontends.
"""

from typing import List, Dict, Optional
import logging
import json
import time
from pathlib import Path

from django.utils import timezone
from django.contrib.gis.geos import GEOSGeometry

from orchestration.models import ProcessingSession
from worldkg_nca.models import OsmEntity
from worldkg_nca.services.wikidata_service import parse_poly_to_wkt
from igea.services.spatial_link_prediction import (
    SpatialLinkPredictionService,
    SPATIAL_LITERAL_TAGS,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional Celery dependency
    from celery import shared_task
    from celery.utils.log import get_task_logger

    logger = get_task_logger(__name__)
except ImportError:  # Fallback so the module still works without Celery
    def shared_task(*dargs, **dkwargs):  # type: ignore
        def decorator(func):
            return func

        # When used as @shared_task without params
        if dargs and callable(dargs[0]) and not dkwargs:
            return decorator(dargs[0])
        return decorator


def _push_step_update(
    session_id: Optional[str],
    name: str,
    status: str,
    message: str,
    pct: int = 0,
) -> None:
    """Send a websocket step_update message if Channels is configured.

    Reuses the existing pipeline_<session_id> group used by the
    WorldKG unified pipeline and country pipeline schematic.
    """
    if not session_id:
        return

    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        group_name = f"pipeline_{session_id}"
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "step_update",
                "step": 8,  # Logical step 8: Predict Spatial Links
                "total": 8,
                "name": name,
                "status": status,
                "message": message,
                "pct": pct,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("USLP Celery: failed to push websocket update: %s", exc)


def _update_session_results(session_id: Optional[str], summary: Dict) -> None:
    """Merge USLP summary metrics into ProcessingSession.results.

    Metrics are stored purely in SQL (JSONField) so they can be
    inspected and visualised later without relying on log files.
    """
    if not session_id:
        return

    try:
        session = ProcessingSession.objects.get(id=session_id)
    except ProcessingSession.DoesNotExist:  # pragma: no cover - safety
        logger.warning("USLP Celery: ProcessingSession %s not found", session_id)
        return

    results = session.results or {}
    # Store under a dedicated key to avoid clobbering existing data
    results["uslp_subgraphs"] = summary
    session.results = results
    session.save(update_fields=["results", "updated_at"])


def _build_head_entities(qs, max_heads: int) -> List[Dict]:
    """Build head entity list from an OsmEntity queryset.

    Mirrors the logic in the predict_spatial_links management command,
    but scoped to a pre-filtered queryset (e.g., polygon geofence).
    """
    head_entities: List[Dict] = []

    spatial_keys = {
        "is_in",
        "is_in:country",
        "is_in:state",
        "is_in:county",
        "addr:country",
        "addr:state",
        "addr:county",
        "addr:city",
        "addr:suburb",
        "addr:hamlet",
        "addr:village",
        "addr:town",
    }

    for e in qs.iterator(chunk_size=10_000):
        tags = e.tags or {}

        # Only include if entity has at least one spatial literal tag
        if not any(k in tags for k in spatial_keys.union(SPATIAL_LITERAL_TAGS)):
            continue

        head_entities.append(
            {
                "osm_id": e.osm_id,
                "lat": e.geom.y,
                "lon": e.geom.x,
                "tags": tags,
            }
        )

        if len(head_entities) >= max_heads:
            logger.info(
                "USLP: Reached max_heads=%d (current queryset truncated)", max_heads
            )
            break

    return head_entities


def _write_links_jsonl(output_path: Path, links: List[Dict]) -> int:
    """Write accepted links to a JSONL file.

    Returns the number of records written.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as f:
        for link in links:
            f.write(json.dumps(link) + "\n")
            count += 1
    return count


@shared_task
def run_uslp_for_subgraph_batch(
    country_name: str,
    subgraphs: List[Dict],
    threshold: float = 0.7,
    top_k: int = 5,
    limit: int = 200_000,
    max_heads: int = 50_000,
    use_gpu: bool = False,
    gpu_device: str = "cuda:0",
    use_fp64: bool = False,
    snapshot_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict:
    """Run USLP spatial link prediction for one or more subgraphs.

    Args:
        country_name: Human-readable country name (e.g., "Tanzania").
        subgraphs: List of {"name", "slug", "poly_path"} dicts.
        threshold: Acceptance threshold (default 0.6).
        top_k: Max links per head entity.
        limit: Max candidate pool size.
        max_heads: Max head entities per subgraph.
        use_gpu: Whether to use GPU-accelerated USLP.
        gpu_device: CUDA device string.
        use_fp64: Use FP64 precision for haversine (GPU only).
        snapshot_id: Optional TemporalSnapshot UUID for scoping.
        session_id: Optional ProcessingSession ID for websocket + metrics.

    Returns:
        Summary dict with per-subgraph metrics and totals.
    """
    start_time = time.time()

    if not subgraphs:
        msg = "No subgraphs provided for USLP batch run"
        logger.warning(msg)
        _push_step_update(session_id, "predict_links", "failed", msg, pct=0)
        return {"success": False, "error": "no_subgraphs"}

    # Initialise USLP service (GPU or CPU)
    service: SpatialLinkPredictionService
    if use_gpu:
        try:
            import torch  # type: ignore

            from igea.services.gpu_uslp_service import GPUAcceleratedUSLP

            if not torch.cuda.is_available():
                logger.warning(
                    "GPU requested but CUDA not available. Falling back to CPU USLP."
                )
                service = SpatialLinkPredictionService()
            else:
                logger.info(f"USLP: Using GPU device {gpu_device} (FP64={use_fp64})")
                service = GPUAcceleratedUSLP(device=gpu_device, use_fp64=use_fp64)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to initialise GPU USLP (%s). Falling back to CPU.", exc
            )
            service = SpatialLinkPredictionService()
    else:
        logger.info("USLP: Using CPU mode")
        service = SpatialLinkPredictionService()

    total_links = 0
    total_saved = 0
    per_subgraph_metrics: List[Dict] = []

    _push_step_update(
        session_id,
        "predict_links",
        "in_progress",
        f"USLP for {len(subgraphs)} subgraphs in {country_name}",
        pct=0,
    )

    for idx, sg in enumerate(subgraphs, start=1):
        name = sg.get("name") or sg.get("slug") or f"subgraph_{idx}"
        slug = sg.get("slug") or name.lower().replace(" ", "_")
        poly_path_str = sg.get("poly_path")

        if not poly_path_str:
            logger.warning("USLP: Subgraph %s has no poly_path; skipping", name)
            per_subgraph_metrics.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": None,
                    "head_count": 0,
                    "links_found": 0,
                    "links_saved": 0,
                    "links_file": None,
                    "error": "missing_poly_path",
                }
            )
            continue

        poly_path = Path(poly_path_str)
        if not poly_path.exists():
            logger.warning("USLP: Poly file not found for %s: %s", name, poly_path)
            per_subgraph_metrics.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": str(poly_path),
                    "head_count": 0,
                    "links_found": 0,
                    "links_saved": 0,
                    "links_file": None,
                    "error": "poly_not_found",
                }
            )
            continue

        logger.info("USLP: Processing subgraph %s (%s)", name, poly_path)

        polygon_wkt = parse_poly_to_wkt(str(poly_path))
        if not polygon_wkt:
            logger.warning("USLP: Failed to parse poly file for %s: %s", name, poly_path)
            per_subgraph_metrics.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": str(poly_path),
                    "head_count": 0,
                    "links_found": 0,
                    "links_saved": 0,
                    "links_file": None,
                    "error": "poly_parse_failed",
                }
            )
            continue

        # Scope head entities by polygon (and optional snapshot)
        qs = OsmEntity.objects.using("vectors").filter(geom__isnull=False)
        if snapshot_id:
            qs = qs.filter(source_snapshot_id=snapshot_id)

        poly_geom = GEOSGeometry(polygon_wkt, srid=4326)
        qs = qs.filter(geom__within=poly_geom)

        head_entities = _build_head_entities(qs, max_heads=max_heads)
        head_count = len(head_entities)
        logger.info(
            "USLP: Subgraph %s — %d head entities with spatial tags",
            name,
            head_count,
        )

        if head_count == 0:
            per_subgraph_metrics.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": str(poly_path),
                    "head_count": 0,
                    "links_found": 0,
                    "links_saved": 0,
                    "links_file": None,
                    "error": "no_head_entities",
                }
            )
            continue

        # Load candidate pool scoped to this subgraph's polygon
        logger.info("USLP: Loading candidate pool for subgraph %s (limit=%d)", name, limit)
        pool_size = service.load_candidate_pool_from_db(limit=limit, polygon_wkt=polygon_wkt)
        logger.info("USLP: Candidate pool size for %s = %d", name, pool_size)

        if pool_size == 0:
            logger.warning("USLP: No candidate entities found for subgraph %s", name)
            per_subgraph_metrics.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": str(poly_path),
                    "head_count": head_count,
                    "pool_size": 0,
                    "links_found": 0,
                    "links_saved": 0,
                    "links_file": None,
                    "error": "no_candidates",
                }
            )
            continue

        # Predict links
        links = service.predict_links_batch(head_entities, threshold=threshold, top_k=top_k)
        links_found = len(links)
        logger.info(
            "USLP: Subgraph %s — %d links found (threshold=%.2f, top_k=%d)",
            name,
            links_found,
            threshold,
            top_k,
        )

        links_saved = 0
        links_file_path: Optional[Path] = None

        if links:
            # Persist to DB (SpatialTripletScore and SpatialTripletScoreRejected)
            links_saved = service.persist_links(links, snapshot_id=snapshot_id, country_name=country_name, threshold=threshold)
            total_saved += links_saved

            # Also write to JSONL file for redundancy / offline analysis
            subgraph_dir = poly_path.parent
            uslp_dir = subgraph_dir / "uslp"
            timestamp_str = timezone.now().strftime("%Y%m%dT%H%M%S")
            links_file_path = uslp_dir / f"uslp_links_{slug}_{timestamp_str}.jsonl"
            written = _write_links_jsonl(links_file_path, links)
            logger.info(
                "USLP: Wrote %d links for %s to %s",
                written,
                name,
                links_file_path,
            )

        total_links += links_found

        per_subgraph_metrics.append(
            {
                "name": name,
                "slug": slug,
                "poly_path": str(poly_path),
                "head_count": head_count,
                "pool_size": pool_size,
                "links_found": links_found,
                "links_saved": links_saved,
                "links_file": str(links_file_path) if links_file_path else None,
            }
        )

        pct = int(100 * idx / max(len(subgraphs), 1))
        _push_step_update(
            session_id,
            "predict_links",
            "in_progress",
            f"USLP subgraph {idx}/{len(subgraphs)}: {name}",
            pct=pct,
        )

    duration = time.time() - start_time

    summary = {
        "success": True,
        "country_name": country_name,
        "pool_size": pool_size,
        "subgraphs": per_subgraph_metrics,
        "total_links": total_links,
        "total_links_saved": total_saved,
        "duration_seconds": duration,
        "threshold": threshold,
        "top_k": top_k,
        "limit": limit,
        "max_heads": max_heads,
        "use_gpu": use_gpu,
        "gpu_device": gpu_device,
    }

    _push_step_update(
        session_id,
        "predict_links",
        "completed",
        f"USLP complete for {len(subgraphs)} subgraphs — {total_saved} links saved",
        pct=100,
    )

    _update_session_results(session_id, summary)

    return summary
