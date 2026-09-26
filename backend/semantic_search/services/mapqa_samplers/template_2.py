"""Template #2 (OBJECT-FIELD-MEASURE) sampler mixin.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from semantic_search.services.mapqa_samplers.constants import (
    FRAMES,
    MAX_PAIR_DISTANCE_KM,
    MIN_PAIR_DISTANCE_KM,
    TEMPLATE_OBJECT_FIELD,
)


class Template2SamplerMixin:
    """Provides ``_sample_template_2`` to MapQAQuestionGenerator."""

    def _sample_template_2(self, country_code, snapshot_id, budget, rng):
        """OBJECT-FIELD-MEASURE: named pair (A, B) 0.5–200 km apart → distance_km."""
        pool = self._named_pool(country_code, snapshot_id, budget)
        if len(pool) < 2:
            return []
        rows = []
        rng.shuffle(pool)
        attempts = 0
        max_attempts = budget * 60
        for a in pool:
            if len(rows) >= budget or attempts >= max_attempts:
                break
            for _ in range(20):
                attempts += 1
                b = rng.choice(pool)
                if b["osm_id"] == a["osm_id"] and b["osm_type"] == a["osm_type"]:
                    continue
                dist_km = self.haversine_km(
                    a["lat"], a["lon"], b["lat"], b["lon"]
                )
                if MIN_PAIR_DISTANCE_KM <= dist_km <= MAX_PAIR_DISTANCE_KM:
                    frame = rng.choice(FRAMES[TEMPLATE_OBJECT_FIELD])
                    question = frame.format(a=a["name"], b=b["name"])
                    rows.append({
                        "template": TEMPLATE_OBJECT_FIELD,
                        "question": question,
                        "answer": f"{round(dist_km, 2)}",
                        "slots": {"a": a["name"], "b": b["name"]},
                        "meta": {
                            "a": a["name"], "b": b["name"],
                            "a_lat": a["lat"], "a_lon": a["lon"],
                            "b_lat": b["lat"], "b_lon": b["lon"],
                            "distance_km": dist_km,
                        },
                    })
                    break
        return rows
