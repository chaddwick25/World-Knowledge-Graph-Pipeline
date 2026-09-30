"""Template #5 (LOCATION-BEARING-CLASSIFY) sampler mixin.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from semantic_search.services.mapqa_samplers.constants import (
    CARDINALS,
    DIRECTION_CONE_DEGREES,
    DIRECTION_NEAREST_RADIUS_M,
    FRAMES,
    MIN_BEARING_PAIR_DISTANCE_M,
    TEMPLATE_LOCATION_BEARING,
)


class Template5SamplerMixin:
    """Provides ``_sample_template_5`` / ``_5a`` / ``_5b``."""

    def _sample_template_5(self, country_code, snapshot_id, budget, rng):
        """LOCATION-BEARING-CLASSIFY: (a) bearing pair; (b) nearest in cone."""
        rows = []
        rows.extend(self._sample_template_5a(country_code, snapshot_id,
                                             budget // 2, rng))
        rows.extend(self._sample_template_5b(country_code, snapshot_id,
                                             budget - budget // 2, rng))
        return rows

    def _sample_template_5a(self, country_code, snapshot_id, budget, rng):
        """(a) pair (X, Y) → bearing → cardinal word (executor convention:
        bearing from X to Y for 'Which direction is X from Y?')."""
        pool = self._named_pool(country_code, snapshot_id, budget)
        if len(pool) < 2:
            return []
        rows = []
        rng.shuffle(pool)
        attempts = 0
        max_attempts = budget * 60
        for x in pool:
            if len(rows) >= budget or attempts >= max_attempts:
                break
            for _ in range(20):
                attempts += 1
                y = rng.choice(pool)
                if self._same_entity(x, y):
                    continue
                dist_m = self.haversine_km(
                    x["lat"], x["lon"], y["lat"], y["lon"]
                ) * 1000
                if dist_m < MIN_BEARING_PAIR_DISTANCE_M:
                    continue
                theta = self.bearing_degrees(x["lat"], x["lon"],
                                             y["lat"], y["lon"])
                cardinal = self.cardinal_from_bearing(theta)
                frame = FRAMES[TEMPLATE_LOCATION_BEARING][0]
                question = frame.format(x=x["name"], y=y["name"])
                rows.append({
                    "template": TEMPLATE_LOCATION_BEARING,
                    "question": question,
                    "answer": cardinal,
                    "slots": {"x": x["name"], "y": y["name"]},
                    "meta": {
                        "x": x["name"], "y": y["name"],
                        "x_lat": x["lat"], "x_lon": x["lon"],
                        "y_lat": y["lat"], "y_lon": y["lon"],
                        "bearing_deg": theta,
                        "cardinal": cardinal,
                    },
                })
                break
        return rows

    def _sample_template_5b(self, country_code, snapshot_id, budget, rng):
        """(b) (amenity, anchor, direction cone ±45°) → nearest in cone → name.

        Anchor-relative: fetch the named-amenity entities within 20 km of the
        anchor once, pick a cardinal direction whose cone contains at least
        one entity, and use the nearest entity in that cone. The nearest
        entity of its own amenity type is guaranteed to be the cone's nearest
        by construction (a closer same-type entity would have been the
        nearest entity).
        """
        pool = self._named_pool(country_code, snapshot_id, budget)
        if not pool:
            return []
        rows = []
        rng.shuffle(pool)
        for anchor in pool:
            if len(rows) >= budget:
                break
            nearby = self._named_amenities_near(
                anchor, DIRECTION_NEAREST_RADIUS_M, country_code, snapshot_id,
                limit=300,
            )
            if not nearby:
                continue
            directions = list(CARDINALS)
            rng.shuffle(directions)
            for cardinal in directions:
                target_deg = CARDINALS.index(cardinal) * 45
                in_cone = [
                    e for e in nearby
                    if self._angular_diff(
                        self.bearing_degrees(anchor["lat"], anchor["lon"],
                                             e["lat"], e["lon"]),
                        target_deg,
                    ) <= DIRECTION_CONE_DEGREES
                ]
                if not in_cone:
                    continue
                nearest = min(in_cone, key=lambda e: e["distance_m"])
                frame = rng.choice(FRAMES[TEMPLATE_LOCATION_BEARING][1:])
                question = frame.format(amenity=nearest["amenity"],
                                        dir=cardinal, name=anchor["name"])
                rows.append({
                    "template": TEMPLATE_LOCATION_BEARING,
                    "question": question,
                    "answer": nearest["name"],
                    "slots": {"amenity": nearest["amenity"], "dir": cardinal,
                              "name": anchor["name"]},
                    "meta": {
                        "anchor": anchor["name"],
                        "amenity": nearest["amenity"],
                        "dir": cardinal,
                        "anchor_lat": anchor["lat"],
                        "anchor_lon": anchor["lon"],
                        "answer": nearest["name"],
                    },
                })
                break
        return rows
