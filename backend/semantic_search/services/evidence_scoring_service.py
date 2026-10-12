"""
EvidenceScoringService: decision coefficients from resolved rows.

The researcher's evidence channels (embedding cosine, class match, USLP
predicted links, probe evidence) combine into three coefficients:

  1. twin score: one calibrated similarity per candidate twin
                        (doppelganger runs), with agreement-based
                        confidence; single-channel runs report None.
  2. presence: Beta-Binomial posterior over "genuinely absent
     posterior          vs under-tagged" for an empty slot, from the
                        probe evidence; gates the LLM replan.
  3. run confidence: one composed confidence line on the done payload,
                        from coverage and channel agreement.

Every coefficient is computed from rows that already exist
([[INV-001]]): the k-NN output, the seed geocode, SpatialTripletScore
USLP links weighted by ProjectionWeightAsset.validation_accuracy, and
the probe digest. Nothing is estimated by the LLM; v0 is deterministic
(no training). The multilevel/ABI calibration of the weights is the
later rung (RESEARCHER_AMPLIFICATION_PLAN.md phase 2), gated on trace
volume.

Fail-soft everywhere: a missing channel is skipped, a failed lookup
degrades the score honestly, and no coefficient ever raises into the
research loop.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Twin channel weights (v0 fixed; phase 2 calibrates them per recipe).
WEIGHTS = {"cosine": 0.6, "class": 0.25, "uslp": 0.15}

# Presence posterior defaults: weak Beta prior (mean 0.2) and a
# conservative absence bar (the posterior upper bound must fall below
# MAX_P before the replan is skipped).
PRIOR_ALPHA = 1.0
PRIOR_BETA = 4.0
MIN_PROBE = 5
MAX_P = 0.10


class EvidenceScoringService:
    """Decision coefficients from resolved rows (deterministic v0)."""

    # ── 1. Twin score ────────────────────────────────────────────────

    @staticmethod
    def score_candidates(seed_class: Optional[str], candidates: list,
                         uslp_by_osm_id: Optional[dict] = None,
                         weights: Optional[dict] = None) -> list:
        """Attach twin_score / twin_confidence to each candidate row.

        Args:
            seed_class: the seed entity's wkg_class (None when unknown).
            candidates: the similarEntities output rows (mutated in
                place; each gains ``twin_score`` and
                ``twin_confidence``).
            uslp_by_osm_id: {candidate osm_id: uslp channel value} where
                the value is normalized_score × validation_accuracy.
            weights: channel weights override (default WEIGHTS).

        Returns:
            The candidates list, re-ranked by twin_score descending.
            Rows keep their original keys; the two new keys are additive.
        """
        w = dict(WEIGHTS if weights is None else weights)
        uslp = uslp_by_osm_id or {}
        for row in candidates:
            channels = {}
            cosine = row.get("similarity")
            if isinstance(cosine, (int, float)) and math.isfinite(cosine):
                channels["cosine"] = max(0.0, min(1.0, float(cosine)))
            if seed_class and row.get("wkg_class"):
                channels["class"] = (
                    1.0 if row["wkg_class"] == seed_class else 0.0
                )
            uslp_val = uslp.get(row.get("osm_id"))
            if isinstance(uslp_val, (int, float)) and math.isfinite(uslp_val):
                channels["uslp"] = max(0.0, min(1.0, float(uslp_val)))

            if not channels:
                row["twin_score"] = None
                row["twin_confidence"] = None
                continue
            total_w = sum(w.get(k, 0.0) for k in channels)
            if total_w <= 0:
                row["twin_score"] = None
                row["twin_confidence"] = None
                continue
            score = sum(
                w.get(k, 0.0) * v for k, v in channels.items()
            ) / total_w
            # Agreement across available channels; a single channel is
            # honest unknown, never faked.
            confidence = (
                round(1.0 - (max(channels.values())
                             - min(channels.values())), 4)
                if len(channels) > 1 else None
            )
            row["twin_score"] = round(score, 4)
            row["twin_confidence"] = confidence
            row["twin_channels"] = sorted(channels)
        candidates.sort(
            key=lambda r: (r["twin_score"] is not None,
                           r["twin_score"] or 0.0),
            reverse=True,
        )
        return candidates

    # ── 2. Presence posterior ────────────────────────────────────────

    @staticmethod
    def presence_posterior(n_class: int, n_total: int,
                           alpha0: float = PRIOR_ALPHA,
                           beta0: float = PRIOR_BETA,
                           min_probe: int = MIN_PROBE,
                           max_p: float = MAX_P) -> dict:
        """Beta-Binomial posterior over "the asked class is present".

        The probe is the evidence: ``n_total`` evidence-worthy entities
        near the anchor, ``n_class`` of them matching the asked class.
        Returns {p_present, n_total, n_class, decision} where decision
        is "absent" (replan cannot help: skip it) or "uncertain".
        """
        n_class = max(0, int(n_class))
        n_total = max(0, int(n_total))
        n_class = min(n_class, n_total)
        alpha = alpha0 + n_class
        beta = beta0 + n_total - n_class
        p = alpha / (alpha + beta)
        sd = math.sqrt(
            alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
        )
        absent = n_total >= min_probe and (p + 2.0 * sd) < max_p
        return {
            "p_present": round(p, 4),
            "n_total": n_total,
            "n_class": n_class,
            "decision": "absent" if absent else "uncertain",
        }

    # ── 3. Run confidence ────────────────────────────────────────────

    @staticmethod
    def compose_confidence(coverage: list,
                           twin_confidences: Optional[list] = None) -> dict:
        """One confidence line for the done payload.

        Base: the coverage ledger's filled fraction. When twin
        confidences are present (doppelganger runs), their mean rides
        along as a component; the band follows the filled fraction.
        """
        rows = [c for c in (coverage or []) if isinstance(c, dict)]
        total = len(rows)
        filled = sum(
            1 for c in rows
            if c.get("status") in ("filled", "filled_by_repair")
        )
        fraction = round(filled / total, 4) if total else 0.0
        agreement = None
        confs = [
            c for c in (twin_confidences or [])
            if isinstance(c, (int, float)) and math.isfinite(c)
        ]
        if confs:
            agreement = round(sum(confs) / len(confs), 4)
        band = "high" if fraction >= 0.8 else (
            "medium" if fraction >= 0.5 else "low"
        )
        return {
            "value": fraction,
            "band": band,
            "components": {
                "filled": filled,
                "total": total,
                "twin_agreement": agreement,
            },
        }

    # ── DB-facing wiring (fail-soft) ─────────────────────────────────

    @classmethod
    def score_preplan_twins(cls, tool_calls: list, country_code: str,
                            snapshot_date: str) -> Optional[list]:
        """Score the pre-plan similarEntities output in place.

        Finds the similarEntities call among the pre-plan tool records,
        geocodes the seed (the tool args carry the name), fetches USLP
        predicted links between the seed and the candidates (weighted by
        the country's ProjectionWeightAsset.validation_accuracy), and
        attaches twin scores. Returns the scored candidate rows, or None
        when there was nothing to score. Never raises.
        """
        try:
            call = next(
                (c for c in (tool_calls or [])
                 if c.get("tool") == "similarEntities"
                 and isinstance(c.get("output"), list) and c["output"]),
                None,
            )
            if call is None:
                return None
            rows = call["output"]
            seed_name = (call.get("args") or {}).get("entity")
            if not seed_name:
                return None

            from semantic_search.services.entity_geocoder import (
                EntityGeocoder,
            )
            seed = EntityGeocoder.geocode(
                seed_name, country_code, snapshot_date,
            )
            if not seed or seed.get("osm_id") is None:
                logger.info(
                    "Twin scoring: seed %r not geocoded; cosine-only",
                    seed_name,
                )
                return cls.score_candidates(None, rows)

            uslp = cls._uslp_channel(
                seed, rows, country_code, snapshot_date,
            )
            return cls.score_candidates(
                seed.get("wkg_class"), rows, uslp_by_osm_id=uslp,
            )
        except Exception as exc:  # noqa: BLE001 (never kill the loop)
            logger.warning("Twin scoring failed (fail-soft): %s", exc)
            return None

    @staticmethod
    def _uslp_channel(seed: dict, rows: list, country_code: str,
                      snapshot_date: str) -> dict:
        """USLP channel values {candidate osm_id: score × val_accuracy}.

        Reads SpatialTripletScore rows where the seed and a candidate
        are the head/tail pair (either direction), predicted=True,
        snapshot-scoped. The channel value is normalized_score scaled by
        the country's ProjectionWeightAsset.validation_accuracy (the
        owner's unused artifact: provenance quality as prior weight).
        Empty dict when anything is missing (channel skipped).
        """
        cand_ids = [
            r.get("osm_id") for r in rows
            if isinstance(r.get("osm_id"), int)
        ]
        if not cand_ids or seed.get("osm_id") is None:
            return {}
        from igea.models import SpatialTripletScore
        from core.models import ProjectionWeightAsset

        val = 1.0
        if country_code:
            asset = (
                ProjectionWeightAsset.objects
                .filter(country_code=country_code,
                        status="COMPLETED")
                .order_by("-updated_at")
                .first()
            )
            if asset and asset.validation_accuracy:
                val = max(0.0, min(1.0, asset.validation_accuracy))

        qs = (
            SpatialTripletScore.objects.using("vectors")
            .filter(predicted=True)
        )
        if snapshot_date:
            qs = qs.filter(snapshot_id=snapshot_date)
        scores = {}
        for row in qs.filter(
            head_osm_id=seed["osm_id"], tail_osm_id__in=cand_ids,
        ).values("tail_osm_id", "normalized_score"):
            scores[row["tail_osm_id"]] = float(row["normalized_score"]) * val
        for row in qs.filter(
            tail_osm_id=seed["osm_id"], head_osm_id__in=cand_ids,
        ).values("head_osm_id", "normalized_score"):
            scores.setdefault(
                row["head_osm_id"],
                float(row["normalized_score"]) * val,
            )
        return scores
