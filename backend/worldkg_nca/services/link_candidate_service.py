from typing import Any, Dict, List, Optional, Tuple
from django.db.models import Q
from core.models import ProjectionWeightAsset
from igea.models import EntityAlignment
from worldkg_nca.models import OsmEntity

class WorldKGLinkCandidateService:
    def __init__(self, country_code: Optional[str] = None) -> None:
        self.country_code = (country_code or "").strip()

    def _get_projection_weights(self) -> Tuple[float, float, float, Dict[str, Any]]:
        w_geo = 1.0
        w_name = 1.0
        w_class = 1.0
        info: Dict[str, Any] = {"source": "default"}

        if self.country_code:
            asset = (
                ProjectionWeightAsset.objects.filter(
                    Q(country_code__iexact=self.country_code)
                    | Q(region_name__iexact=self.country_code),
                    status=ProjectionWeightAsset.Status.COMPLETED,
                )
                .order_by("-updated_at")
                .first()
            )
            if asset:
                w_geo = float(asset.w_geo)
                w_name = float(asset.w_name)
                w_class = float(asset.w_class)
                info = {
                    "source": "learned",
                    "asset_id": str(asset.id),
                    "validation_accuracy": float(asset.validation_accuracy),
                    "sample_count": int(asset.sample_count),
                }

        return w_geo, w_name, w_class, info

    def get_alignment_candidates(
        self, osm_entity: OsmEntity, top_k: int = 10
    ) -> Dict[str, Any]:
        w_geo, w_name, w_class, weights_info = self._get_projection_weights()

        alignments = (
            EntityAlignment.objects.filter(
                osm_type=osm_entity.osm_type,
                osm_id=osm_entity.osm_id,
            )
            .order_by("-confidence", "-created_at")
        )

        denom = w_geo + w_name + w_class
        if denom <= 0:
            denom = 1.0

        candidates: List[Dict[str, Any]] = []

        for alignment in alignments:
            name_score = float(alignment.confidence or 0.0)

            geo_score = 0.0
            if alignment.distance_meters is not None:
                try:
                    dist_km = float(alignment.distance_meters) / 1000.0
                    geo_score = 1.0 / (1.0 + dist_km)
                except (TypeError, ValueError):
                    geo_score = 0.0

            class_score = 1.0

            final_raw = (w_geo * geo_score) + (w_name * name_score) + (w_class * class_score)
            final_score = final_raw / denom

            uri = alignment.wikidata_uri
            wikidata_id = None
            if uri:
                parts = uri.rstrip("/").split("/")
                if parts:
                    wikidata_id = parts[-1]

            candidate: Dict[str, Any] = {
                "wikidata_uri": uri,
                "wikidata_id": wikidata_id,
                "label": alignment.wikidata_label,
                "scores": {
                    "name_score": name_score,
                    "geo_score": geo_score,
                    "class_score": class_score,
                    "final_score": final_score,
                },
                "alignment": {
                    "method": alignment.alignment_method,
                    "confidence": float(alignment.confidence),
                    "distance_meters": alignment.distance_meters,
                    "iteration": alignment.iteration,
                },
                "source": "igea_alignment",
            }
            candidates.append(candidate)

        candidates.sort(key=lambda c: c["scores"]["final_score"], reverse=True)
        if top_k and top_k > 0:
            candidates = candidates[:top_k]

        projection_weights = {
            "w_geo": w_geo,
            "w_name": w_name,
            "w_class": w_class,
            **weights_info,
        }

        return {
            "projection_weights": projection_weights,
            "candidates": candidates,
        }
