import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from django.conf import settings
from django.db.models import Q

from core.services.pipeline.base_service import BaseService
from core.models import ProjectionWeightAsset
from igea.models import SpatialTripletScore, SpatialTripletScoreRejected


logger = logging.getLogger(__name__)


class ProjectionWeightService(BaseService):
    """Service to learn country-specific projection weights for tri-space scores.

    Uses SpatialTripletScore (accepted) and SpatialTripletScoreRejected (rejected)
    as a weak ground truth to learn weights (w_geo, w_name, w_class) that
    maximize F1 at the existing acceptance threshold.
    """

    def execute(self, config: Dict) -> Dict:
        country_name = (config.get("country_name") or "").strip()
        iso_code = (config.get("iso_code") or "").strip().upper()
        grid_size = int(config.get("grid_size", 4))
        min_weight = float(config.get("min_weight", 0.5))
        max_weight = float(config.get("max_weight", 2.0))
        max_samples = int(config.get("max_samples", 50000))
        output_root = config.get("output_path")

        if not output_root:
            base_dir = Path(getattr(settings, "BASE_DIR", Path.cwd()))
            output_root = base_dir.parent / "data" / "projection_weights"
        else:
            output_root = Path(output_root)

        if not country_name and not iso_code:
            raise ValueError("ProjectionWeightService requires country_name or iso_code")

        country_key = iso_code or country_name
        self.logger.info(
            "Learning projection weights for country=%s (grid_size=%s, range=[%s,%s])",
            country_key,
            grid_size,
            min_weight,
            max_weight,
        )

        # ------------------------------------------------------------------
        # 1) Load validation data from DB (accepted vs rejected links)
        # ------------------------------------------------------------------
        country_filter = Q()
        if country_name:
            from core.services.snapshot.regional_path_service import normalize_country_slug
            normalized = normalize_country_slug(country_name)
            country_filter |= Q(country_name__iexact=country_name)
            country_filter |= Q(country_name__iexact=normalized)
        if iso_code and iso_code != country_name:
            country_filter |= Q(country_name__iexact=iso_code)

        positives_qs = SpatialTripletScore.objects.filter(country_filter).only(
            "geo_score", "name_score", "topo_score"
        )
        negatives_qs = SpatialTripletScoreRejected.objects.filter(country_filter).only(
            "geo_score", "name_score", "topo_score"
        )

        positives = list(
            positives_qs.values_list("geo_score", "name_score", "topo_score")[:max_samples]
        )
        negatives = list(
            negatives_qs.values_list("geo_score", "name_score", "topo_score")[:max_samples]
        )

        if not positives or not negatives:
            self.logger.warning(
                "Not enough validation data for %s (positives=%d, negatives=%d)",
                country_key,
                len(positives),
                len(negatives),
            )
            # Keep any existing asset but do not create a new one
            return {
                "country_code": iso_code or country_name,
                "region_name": country_name or iso_code,
                "created": False,
                "weights": None,
                "validation_accuracy": 0.0,
                "sample_count": len(positives) + len(negatives),
            }

        validation_data: List[Tuple[float, float, float, int]] = []
        for g, n, t in positives:
            validation_data.append((float(g), float(n), float(t), 1))
        for g, n, t in negatives:
            validation_data.append((float(g), float(n), float(t), 0))

        # ------------------------------------------------------------------
        # 2) Optimize weights via simple grid search
        # ------------------------------------------------------------------
        w_geo, w_name, w_class, best_f1 = self._optimize_weights(
            validation_data,
            grid_size=grid_size,
            min_weight=min_weight,
            max_weight=max_weight,
        )

        self.logger.info(
            "Best weights for %s: w_geo=%.3f, w_name=%.3f, w_class=%.3f (F1=%.4f)",
            country_key,
            w_geo,
            w_name,
            w_class,
            best_f1,
        )

        # ------------------------------------------------------------------
        # 3) Persist to filesystem + DB asset
        # ------------------------------------------------------------------
        output_root.mkdir(parents=True, exist_ok=True)
        country_slug = country_key.lower().replace(" ", "_")
        asset_path = output_root / f"{country_slug}_weights.json"

        payload = {
            "country_code": iso_code or country_name,
            "region_name": country_name or iso_code,
            "w_geo": w_geo,
            "w_name": w_name,
            "w_class": w_class,
            "validation_accuracy": best_f1,
            "sample_count": len(validation_data),
        }

        try:
            with asset_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
        except Exception as exc:
            self.logger.warning("Failed to write projection weight file %s: %s", asset_path, exc)

        asset, created = ProjectionWeightAsset.objects.update_or_create(
            country_code=payload["country_code"],
            region_name=payload["region_name"],
            defaults={
                "asset_path": str(asset_path),
                "w_geo": w_geo,
                "w_name": w_name,
                "w_class": w_class,
                "validation_accuracy": best_f1,
                "sample_count": len(validation_data),
                "status": ProjectionWeightAsset.Status.COMPLETED,
                "metadata": {
                    "grid_size": grid_size,
                    "min_weight": min_weight,
                    "max_weight": max_weight,
                },
            },
        )

        return {
            "country_code": payload["country_code"],
            "region_name": payload["region_name"],
            "created": created,
            "weights": {
                "w_geo": w_geo,
                "w_name": w_name,
                "w_class": w_class,
            },
            "validation_accuracy": best_f1,
            "sample_count": len(validation_data),
            "asset_id": str(asset.id),
            "asset_path": str(asset_path),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _optimize_weights(
        data: Sequence[Tuple[float, float, float, int]],
        grid_size: int,
        min_weight: float,
        max_weight: float,
    ) -> Tuple[float, float, float, float]:
        """Grid search over weight combinations to maximize F1.

        Args:
            data: Iterable of (geo_score, name_score, topo_score, label)
            grid_size: Number of candidate values per weight dimension
            min_weight: Minimum weight
            max_weight: Maximum weight

        Returns:
            (w_geo, w_name, w_class, best_f1)
        """
        if grid_size < 1:
            grid_size = 1
        if max_weight < min_weight:
            max_weight = min_weight

        # Simple linear grid
        if grid_size == 1:
            candidates = [min_weight]
        else:
            step = (max_weight - min_weight) / (grid_size - 1)
            candidates = [min_weight + i * step for i in range(grid_size)]

        best_f1 = 0.0
        best_weights = (1.0, 1.0, 1.0)

        for w_geo in candidates:
            for w_name in candidates:
                for w_class in candidates:
                    f1 = ProjectionWeightService._evaluate_weights(data, w_geo, w_name, w_class)
                    if f1 > best_f1:
                        best_f1 = f1
                        best_weights = (w_geo, w_name, w_class)

        return (*best_weights, best_f1)

    @staticmethod
    def _evaluate_weights(
        data: Iterable[Tuple[float, float, float, int]],
        w_geo: float,
        w_name: float,
        w_class: float,
        threshold: float = 0.7,
    ) -> float:
        """Compute F1 score for a given weight triple.

        Args:
            data: Iterable of (geo_score, name_score, topo_score, label)
            w_geo: Weight for geo_score
            w_name: Weight for name_score
            w_class: Weight for topo/class score
            threshold: Decision threshold on weighted sum
        """
        tp = fp = fn = 0

        # Normalize by sum of weights so that threshold is applied on a
        # [0, 1]-like scale, mirroring the original normalized_score logic
        denom = w_geo + w_name + w_class
        if denom <= 0:
            denom = 1.0

        for geo_score, name_score, topo_score, label in data:
            raw = (
                w_geo * float(geo_score)
                + w_name * float(name_score)
                + w_class * float(topo_score)
            )
            score = raw / denom
            pred = 1 if score >= threshold else 0

            if pred == 1 and label == 1:
                tp += 1
            elif pred == 1 and label == 0:
                fp += 1
            elif pred == 0 and label == 1:
                fn += 1

        if tp == 0:
            return 0.0

        precision = tp / float(tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / float(tp + fn) if (tp + fn) > 0 else 0.0
        if precision == 0.0 or recall == 0.0:
            return 0.0

        return 2.0 * precision * recall / (precision + recall)
