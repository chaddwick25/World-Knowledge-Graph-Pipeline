import logging
from typing import Optional, Dict
from django.db import transaction
from worldkg_nca.models import OsmEntity, PrecomputedLinkCandidate
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
logger = logging.getLogger(__name__)


def run_precompute_for_region(
    country_name: str,
    iso_code: Optional[str] = None,
    top_k: int = 10,
    batch_size: int = 500,
    max_entities: Optional[int] = None,
) -> Dict[str, int]:
    country_code = (iso_code or country_name or "").strip()
    service = WorldKGLinkCandidateService(country_code=country_code)

    qs = OsmEntity.objects.using("vectors").filter(geom__isnull=False)

    processed = 0
    for entity in qs.iterator(chunk_size=batch_size):
        if max_entities is not None and processed >= max_entities:
            break

        result = service.get_alignment_candidates(entity, top_k=top_k)
        candidates = result.get("candidates") or []
        projection_weights = result.get("projection_weights") or {}
        asset_id = projection_weights.get("asset_id")

        with transaction.atomic(using="vectors"):
            PrecomputedLinkCandidate.objects.using("vectors").update_or_create(
                osm_type=entity.osm_type,
                osm_id=entity.osm_id,
                projection_asset_id=asset_id,
                defaults={
                    "country_code": country_code,
                    "candidates": candidates,
                },
            )

        processed += 1

        if processed % max(1, batch_size) == 0:
            logger.info(
                "Precomputed link candidates for %d entities (country=%s)",
                processed,
                country_code,
            )

    return {
        "processed": processed,
        "country_code": country_code,
        "top_k": top_k,
    }
