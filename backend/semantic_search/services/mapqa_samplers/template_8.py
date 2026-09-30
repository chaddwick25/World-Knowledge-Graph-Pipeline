"""Template #8 (PLACE-ATTRIBUTE-QUERY) sampler mixin.

Extracted from ``mapqa_question_generator.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from semantic_search.services.mapqa_samplers.constants import (
    ADJACENCY_RADIUS_M,
    FRAMES,
    TEMPLATE_PLACE_ATTRIBUTE,
)


class Template8SamplerMixin:
    """Provides ``_sample_template_8`` / ``_8a`` / ``_8b``."""

    def _sample_template_8(self, country_code, snapshot_id, budget, rng):
        """PLACE-ATTRIBUTE-QUERY: (a) named entity → amenity attribute;
        (b) (amenity, anchor) adjacency at 50m → name."""
        rows = []
        rows.extend(self._sample_template_8a(country_code, snapshot_id,
                                             budget // 2, rng))
        rows.extend(self._sample_template_8b(country_code, snapshot_id,
                                             budget - budget // 2, rng))
        return rows

    def _sample_template_8a(self, country_code, snapshot_id, budget, rng):
        """(a) named entity with tags.amenity → the amenity value."""
        pool = self._named_pool(country_code, snapshot_id, budget)
        rows = []
        rng.shuffle(pool)
        for entity in pool:
            if len(rows) >= budget:
                break
            amenity = (entity["tags"] or {}).get("amenity")
            if not amenity:
                continue
            frame = rng.choice(FRAMES[TEMPLATE_PLACE_ATTRIBUTE][:2])
            question = frame.format(name=entity["name"])
            rows.append({
                "template": TEMPLATE_PLACE_ATTRIBUTE,
                "question": question,
                "answer": amenity,
                "slots": {"name": entity["name"]},
                "meta": {
                    "name": entity["name"],
                    "amenity": amenity,
                    "lat": entity["lat"],
                    "lon": entity["lon"],
                },
            })
        return rows

    def _sample_template_8b(self, country_code, snapshot_id, budget, rng):
        """(b) (amenity, anchor) → nearest amenity entity within 50m → name.

        Anchor-relative: pick a named amenity entity within 50 m of the
        anchor, then resolve the exact nearest of its amenity type within
        50 m (covers the edge where an unnamed closer entity of the same
        type exists) — the plan's ST_DWithin 50m excl. self, LIMIT 1.
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
                anchor, ADJACENCY_RADIUS_M, country_code, snapshot_id,
                limit=10,
            )
            if not nearby:
                continue
            e = rng.choice(nearby)
            name = self._nearest_amenity(
                anchor, e["amenity"], country_code, snapshot_id,
                max_radius_m=ADJACENCY_RADIUS_M,
            )
            if not name:
                continue
            frame = rng.choice(FRAMES[TEMPLATE_PLACE_ATTRIBUTE][2:])
            question = frame.format(amenity=e["amenity"], name=anchor["name"])
            rows.append({
                "template": TEMPLATE_PLACE_ATTRIBUTE,
                "question": question,
                "answer": name,
                "slots": {"amenity": e["amenity"], "name": anchor["name"]},
                "meta": {
                    "anchor": anchor["name"],
                    "amenity": e["amenity"],
                    "anchor_lat": anchor["lat"],
                    "anchor_lon": anchor["lon"],
                    "answer": name,
                },
            })
        return rows
