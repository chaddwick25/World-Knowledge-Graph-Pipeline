"""Template #1 (FILTER-AGGREGATE-MEASURE) sampler mixin.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from semantic_search.services.mapqa_samplers.constants import (
    FRAMES,
    RADII_M,
    TEMPLATE_FILTER_AGGREGATE,
)


class Template1SamplerMixin:
    """Provides ``_sample_template_1`` to MapQAQuestionGenerator."""

    def _sample_template_1(self, country_code, snapshot_id, budget, rng):
        """FILTER-AGGREGATE-MEASURE: (amenity, named anchor, radius) → count.

        Anchor-relative sampling: choose the anchor first, then pick an
        amenity that actually has entities within the chosen radius (from the
        grouped ST_DWithin count query) — guarantees count ≥ 1 by
        construction instead of burning random (amenity, radius) draws.
        """
        pool = self._named_pool(country_code, snapshot_id, budget)
        if not pool:
            return []
        rows = []
        rng.shuffle(pool)
        for anchor in pool:
            if len(rows) >= budget:
                break
            for radius_m in rng.sample(RADII_M, len(RADII_M)):
                nearby = self._amenities_near(
                    anchor, radius_m, country_code, snapshot_id,
                    named_only=False, limit=20,
                )
                if not nearby:
                    continue
                amenity, count = rng.choice(nearby)
                if count < 1:
                    continue
                frame = rng.choice(FRAMES[TEMPLATE_FILTER_AGGREGATE])
                question = frame.format(amenity=amenity, name=anchor["name"],
                                        r=radius_m)
                rows.append({
                    "template": TEMPLATE_FILTER_AGGREGATE,
                    "question": question,
                    "answer": str(count),
                    "slots": {
                        "amenity": amenity,
                        "name": anchor["name"],
                        "r": f"{radius_m}m",
                    },
                    "meta": {
                        "anchor": anchor["name"],
                        "amenity": amenity,
                        "radius_m": radius_m,
                        "anchor_lat": anchor["lat"],
                        "anchor_lon": anchor["lon"],
                        "count": count,
                    },
                })
                break
        return rows
