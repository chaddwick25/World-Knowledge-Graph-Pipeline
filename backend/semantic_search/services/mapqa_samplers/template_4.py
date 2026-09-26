"""Template #4 (GEOCODE-BATCH-COMPARE) sampler mixin.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from semantic_search.services.mapqa_samplers.constants import (
    DIRECTION_NEAREST_RADIUS_M,
    FRAMES,
    TEMPLATE_GEOCODE_BATCH,
)


class Template4SamplerMixin:
    """Provides ``_sample_template_4`` / ``_4a`` / ``_4b``."""

    def _sample_template_4(self, country_code, snapshot_id, budget, rng):
        """GEOCODE-BATCH-COMPARE: (a) triple argmin; (b) nearest amenity."""
        rows = []
        rows.extend(self._sample_template_4a(country_code, snapshot_id,
                                             budget // 2, rng))
        rows.extend(self._sample_template_4b(country_code, snapshot_id,
                                             budget - budget // 2, rng))
        return rows

    def _sample_template_4a(self, country_code, snapshot_id, budget, rng):
        """(a) anchor Z + candidates X, Y → name of the closer candidate."""
        pool = self._named_pool(country_code, snapshot_id, budget)
        if len(pool) < 3:
            return []
        rows = []
        rng.shuffle(pool)
        attempts = 0
        max_attempts = max(budget * 60, 200)
        for z in pool:
            if len(rows) >= budget or attempts >= max_attempts:
                break
            for _ in range(20):
                attempts += 1
                x = rng.choice(pool)
                y = rng.choice(pool)
                if self._same_entity(x, z) or self._same_entity(y, z) or \
                        self._same_entity(x, y):
                    continue
                d_x = self.haversine_km(
                    z["lat"], z["lon"], x["lat"], x["lon"]
                ) * 1000
                d_y = self.haversine_km(
                    z["lat"], z["lon"], y["lat"], y["lon"]
                ) * 1000
                if abs(d_x - d_y) < 1.0:
                    continue  # too ambiguous
                winner = x if d_x < d_y else y
                frame = rng.choice(FRAMES[TEMPLATE_GEOCODE_BATCH][:2])
                question = frame.format(z=z["name"], x=x["name"], y=y["name"])
                rows.append({
                    "template": TEMPLATE_GEOCODE_BATCH,
                    "question": question,
                    "answer": winner["name"],
                    "slots": {"z": z["name"], "x": x["name"], "y": y["name"]},
                    "meta": {
                        "z": z["name"], "x": x["name"], "y": y["name"],
                        "z_lat": z["lat"], "z_lon": z["lon"],
                        "x_lat": x["lat"], "x_lon": x["lon"],
                        "y_lat": y["lat"], "y_lon": y["lon"],
                        "winner": winner["name"],
                    },
                })
                break
        return rows

    def _sample_template_4b(self, country_code, snapshot_id, budget, rng):
        """(b) (amenity, anchor) → nearest entity of the type → name.

        Anchor-relative: pick the amenity from the named-amenity pool within
        20 km of the anchor, then resolve the true (unbounded) nearest named
        entity of that type — high hit rate, exact ground truth.
        """
        pool = self._named_pool(country_code, snapshot_id, budget)
        if not pool:
            return []
        rows = []
        rng.shuffle(pool)
        for anchor in pool:
            if len(rows) >= budget:
                break
            nearby = self._amenities_near(
                anchor, DIRECTION_NEAREST_RADIUS_M, country_code, snapshot_id,
                named_only=True, limit=20,
            )
            if not nearby:
                continue
            amenity = rng.choice(nearby)[0]
            name = self._nearest_amenity(
                anchor, amenity, country_code, snapshot_id
            )
            if not name:
                continue
            frame = rng.choice(FRAMES[TEMPLATE_GEOCODE_BATCH][2:])
            question = frame.format(amenity=amenity, name=anchor["name"])
            rows.append({
                "template": TEMPLATE_GEOCODE_BATCH,
                "question": question,
                "answer": name,
                "slots": {"amenity": amenity, "name": anchor["name"]},
                "meta": {
                    "anchor": anchor["name"],
                    "amenity": amenity,
                    "anchor_lat": anchor["lat"],
                    "anchor_lon": anchor["lon"],
                    "answer": name,
                },
            })
        return rows
