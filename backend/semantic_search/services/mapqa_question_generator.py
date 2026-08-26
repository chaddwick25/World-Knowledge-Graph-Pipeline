"""
mapqa_question_generator.py — self-supervised MapQA question–answer generation.

Generates training rows for the 5 existing MapQA macro-templates
(#1 FILTER-AGGREGATE-MEASURE, #2 OBJECT-FIELD-MEASURE, #4 GEOCODE-BATCH-COMPARE,
#5 LOCATION-BEARING-CLASSIFY, #8 PLACE-ATTRIBUTE-QUERY) from ``OsmEntity``
ground truth in the vectors DB — no graph/factor-table dependency.

Every answer is computed exactly from PostGIS at generation time
(``ST_DWithin`` counts, haversine distances, nearest-entity ordering,
bearing → cardinal) and can be recomputed at test time. Generation is
deterministic for a given (seed, snapshot, DB state): pool selection uses
md5-hash ordering and all sampling goes through a seeded ``random.Random``.

Sampling rules ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §3):
- Only named entities (``tags->>'name' IS NOT NULL``) with valid, non-NaN
  coordinates.
- Names containing CSV-breaking characters (quotes, commas) are excluded from
  sampling so the frame text stays identical to the DB name (geocode-ability);
  :meth:`sanitize_name` remains as a safety net.
- Generations whose ground-truth answer is empty (0 results, no nearest, no
  amenity tag) are skipped — the raw dataset's empty-answer rows are a known
  quality bug, not to be reproduced.
- Dedupe on normalized question text.
- Per (template × country) budget via ``per_class``; a stratified 20% holdout
  slice is marked ``self_supervised_test`` by :meth:`split_holdout`.
"""

import json
import logging
import math
import random
import re
from pathlib import Path

from django.conf import settings
from django.db import connections

from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)

# ── Template names ─────────────────────────────────────────────────────────
TEMPLATE_FILTER_AGGREGATE = "FILTER-AGGREGATE-MEASURE (#1)"
TEMPLATE_OBJECT_FIELD = "OBJECT-FIELD-MEASURE (#2)"
TEMPLATE_GEOCODE_BATCH = "GEOCODE-BATCH-COMPARE (#4)"
TEMPLATE_LOCATION_BEARING = "LOCATION-BEARING-CLASSIFY (#5)"
TEMPLATE_PLACE_ATTRIBUTE = "PLACE-ATTRIBUTE-QUERY (#8)"

TEMPLATE_BY_NUMBER = {
    1: TEMPLATE_FILTER_AGGREGATE,
    2: TEMPLATE_OBJECT_FIELD,
    4: TEMPLATE_GEOCODE_BATCH,
    5: TEMPLATE_LOCATION_BEARING,
    8: TEMPLATE_PLACE_ATTRIBUTE,
}

# Concept-transformation / metric strings (mirror train_mapqa_parser.TEMPLATE_MAP)
TEMPLATE_META = {
    TEMPLATE_FILTER_AGGREGATE: (
        "OBJECT(anchor) + SUB_COND(radius) → SUPPORT(within_radius) → MEASURE",
        "Count of amenity entities within a radius",
    ),
    TEMPLATE_OBJECT_FIELD: (
        "OBJECT(a) + OBJECT(b) → FIELD(haversine distance) → MEASURE",
        "Distance in kilometers between two entities",
    ),
    TEMPLATE_GEOCODE_BATCH: (
        "LOCATIONs → coordinates → SUPPORT(distance) × 2 → MEASURE(argmin)",
        "Closer candidate / nearest entity of a given type",
    ),
    TEMPLATE_LOCATION_BEARING: (
        "SUB_COND(direction) + SUPPORT(nearest in direction) → MEASURE",
        "Cardinal direction / nearest entity in a direction cone",
    ),
    TEMPLATE_PLACE_ATTRIBUTE: (
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(adjacency) → MEASURE",
        "Amenity attribute / adjacent entity of a given type",
    ),
}

# Question frames per template (slots: {amenity}, {name}, {a}, {b}, {x}, {y},
# {z}, {r}, {dir}) — reproduces the lexical patterns of the MapQA-llm data
# ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §4).
FRAMES = {
    TEMPLATE_FILTER_AGGREGATE: [
        "What are the {amenity} within {r}m of {name}?",
        "Which {amenity} are within {r}m of {name}?",
        "What {amenity} can be found within {r}m of {name}?",
        "List the {amenity} within {r}m of {name}.",
    ],
    TEMPLATE_OBJECT_FIELD: [
        "How far is {a} from {b}?",
        "What is the distance between {a} and {b}?",
        "How far apart are {a} and {b}?",
    ],
    TEMPLATE_GEOCODE_BATCH: [
        "Which is closer to {z}, {x} or {y}?",
        "Which spot is closer to {z}, {x} or {y}?",
        "What is the nearest {amenity} to {name}?",
        "What is the closest {amenity} around {name}?",
    ],
    TEMPLATE_LOCATION_BEARING: [
        "Which direction is {x} from {y}?",
        "What is the nearest {amenity} {dir} of {name}?",
        "What is the closest {amenity} {dir} of {name}?",
    ],
    TEMPLATE_PLACE_ATTRIBUTE: [
        "What amenity is available at {name}?",
        "What amenity is present in {name}?",
        "What {amenity} is adjacent to {name}?",
        "What {amenity} is right by {name}?",
        "What {amenity} is beside {name}?",
    ],
}

# Radius choices for #1 (meters) — [MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §3
RADII_M = (50, 100, 200, 500)

# 8-way cardinals (full words; matches the executor's N/NE/... sectors)
CARDINALS = ["north", "northeast", "east", "southeast",
             "south", "southwest", "west", "northwest"]

# Per-question-type labels written to the CSV (informational — the parser
# trains on question text + Macro-template, not this column).
QUESTION_TYPES = {
    TEMPLATE_FILTER_AGGREGATE: "self_supervised_radius_count",
    TEMPLATE_OBJECT_FIELD: "self_supervised_distance",
    TEMPLATE_GEOCODE_BATCH: "self_supervised_nearest",
    TEMPLATE_LOCATION_BEARING: "self_supervised_bearing",
    TEMPLATE_PLACE_ATTRIBUTE: "self_supervised_attribute",
}

# Canonical amenity vocabulary fallback (mirrors train_mapqa_parser.OBJECT_SIGNALS)
COMMON_AMENITIES = {
    "bar", "restaurant", "cafe", "hotel", "school", "hospital", "shop",
    "amenity", "pub", "bank", "pharmacy", "park", "church", "library",
    "cinema", "theatre", "gas", "fuel", "parking", "toilet", "atm",
    "bus_station", "fast_food", "place_of_worship", "clinic", "police",
    "post_office", "car_rental", "shelter", "telephone", "bench",
}

# Pool sizing: how many candidate entities to fetch per template before
# sampling with the seeded RNG.
POOL_PER_CLASS_MULTIPLIER = 12
MIN_POOL_SIZE = 120
MAX_POOL_SIZE = 2000

# Distance-range gate for #2 (km) and minimum separation for #5a (m)
MIN_PAIR_DISTANCE_KM = 0.5
MAX_PAIR_DISTANCE_KM = 200.0
MIN_BEARING_PAIR_DISTANCE_M = 100.0

# Direction cone width for #5b (degrees, ± around the cardinal axis)
DIRECTION_CONE_DEGREES = 45
DIRECTION_NEAREST_RADIUS_M = 20000

# Adjacency radius for #8b (plan: 50m)
ADJACENCY_RADIUS_M = 50


def sanitize_name(name: str) -> str:
    """Strip characters that break CSV/frames (double quotes, commas).

    Single apostrophes are preserved — they are CSV-safe and keep the
    question text resolvable by the executor's ILIKE fallback
    ("Surfdog's Java Hot" stays geocode-able).
    """
    if not name:
        return ""
    name = name.replace('"', "").replace(",", "")
    name = re.sub(r"\s+", " ", name).strip()
    return name


class MapQAQuestionGenerator:
    """Deterministic self-supervised MapQA row generator.

    Public API:
      - :meth:`generate` — rows for one country/snapshot (Region=self_supervised)
      - :meth:`split_holdout` — stratified 20% per-template holdout slice
      - :meth:`sanitize_name` / :meth:`bearing_degrees` / :meth:`haversine_km`
        — shared helpers (also used by the paraphraser and tests)
    """

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)
        self._vocab = None

    # ── Public API ──────────────────────────────────────────────────────────

    def generate(self, country_code: str, snapshot_id: str = None,
                 templates: tuple = (1, 2, 4, 5, 8),
                 per_class: int = 250,
                 pipeline_run_id: str = "",
                 paraphraser=None,
                 paraphrase_per_seed: int = 2) -> list:
        """Generate CSV-ready row dicts for one country/snapshot.

        Rows are deduplicated on normalized question text and capped at
        ``per_class`` per (template × country). Region is ``self_supervised``;
        call :meth:`split_holdout` to carve the test slice.

        If ``paraphraser`` is given (a :class:`MapQAParaphraser`), up to
        ``paraphrase_per_seed`` slot-validated paraphrases are emitted per
        seed row with ``Question type=self_supervised_paraphrase``.
        """
        country_code = (country_code or "").strip().upper()
        snapshot_id = snapshot_id or get_latest_snapshot_id()
        if not country_code or not snapshot_id:
            logger.warning("generate: country=%r snapshot=%r — aborting",
                           country_code, snapshot_id)
            return []

        # One RNG per generate() call so repeated calls are identical
        # regardless of prior calls (determinism).
        rng = random.Random(self.seed)
        all_rows = []
        for template_num in templates:
            sampler = self._sampler(template_num)
            if sampler is None:
                logger.warning("generate: unknown template %r — skipping", template_num)
                continue
            rows = sampler(country_code, snapshot_id, per_class, rng)
            logger.info(
                "Generated %d rows for %s template #%s",
                len(rows), country_code, template_num,
            )
            all_rows.extend(rows)

        # Dedupe on normalized question text (keep first occurrence), then
        # convert to CSV rows — adding slot-validated paraphrases per seed.
        seen = set()
        out = []
        for row in all_rows:
            key = self.normalize_question(row["question"])
            if key in seen:
                continue
            seen.add(key)
            csv_row = self._to_csv_row(row, country_code, snapshot_id,
                                       pipeline_run_id)
            out.append(csv_row)
            if paraphraser is not None:
                slots = list(row.get("slots", {}).values())
                emitted = 0
                for para in paraphraser.paraphrase(row["question"], slots):
                    if emitted >= paraphrase_per_seed:
                        break
                    p_key = self.normalize_question(para)
                    if p_key in seen or p_key == key:
                        continue
                    seen.add(p_key)
                    p_row = dict(csv_row)
                    p_row["MapQA question"] = para
                    p_row["Question type"] = "self_supervised_paraphrase"
                    out.append(p_row)
                    emitted += 1
        return out

    def split_holdout(self, rows: list, holdout_frac: float = 0.2) -> list:
        """Mark a stratified (per-template) holdout slice ``self_supervised_test``.

        Mutates and returns ``rows``. Stratification keeps the per-class
        distribution of the holdout identical to the batch so per-class
        metrics are meaningful.
        """
        if holdout_frac <= 0:
            return rows
        by_template = {}
        for row in rows:
            by_template.setdefault(row["Macro-template"], []).append(row)
        for template, template_rows in by_template.items():
            rng = random.Random(f"holdout:{self.seed}:{template}")
            rng.shuffle(template_rows)
            n_test = max(1, int(round(len(template_rows) * holdout_frac)))
            for row in template_rows[:n_test]:
                row["Region"] = "self_supervised_test"
        return rows

    # ── Samplers ────────────────────────────────────────────────────────────

    def _sampler(self, template_num: int):
        samplers = {
            1: self._sample_template_1,
            2: self._sample_template_2,
            4: self._sample_template_4,
            5: self._sample_template_5,
            8: self._sample_template_8,
        }
        return samplers.get(template_num)

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

    # ── Data access (vectors DB) ───────────────────────────────────────────

    def _named_pool(self, country_code, snapshot_id, budget) -> list:
        """Deterministic pseudo-random pool of named entities with coords.

        ``ORDER BY md5(...)`` gives a stable hash-based ordering (unlike
        ``ORDER BY random()``) so the pool — and therefore the generated
        questions — is identical across runs with the same seed + DB state.
        """
        limit = max(MIN_POOL_SIZE, min(
            budget * POOL_PER_CLASS_MULTIPLIER, MAX_POOL_SIZE
        ))
        from django.db.models import CharField, FloatField
        from django.db.models.expressions import RawSQL
        qs = (
            OsmEntity.objects.using("vectors")
            .filter(snapshot_id=snapshot_id,
                    country_code=country_code,
                    tags__name__isnull=False)
            .exclude(geom__isnull=True)
            .annotate(
                lat=RawSQL("ST_Y(geom)", [], output_field=FloatField()),
                lon=RawSQL("ST_X(geom)", [], output_field=FloatField()),
                _pool_hash=RawSQL(
                    "md5(concat(osm_type, ':', osm_id::text))", [],
                    output_field=CharField(),
                ),
            )
            .order_by("_pool_hash")
        )
        try:
            entities = list(qs[:limit])
        except Exception as exc:
            logger.warning("_named_pool failed for %s/%s: %s",
                           country_code, snapshot_id, exc)
            return []
        pool = []
        for e in entities:
            lat, lon = e.lat, e.lon
            if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
                continue
            name = (e.tags or {}).get("name")
            if not name:
                continue
            name = sanitize_name(name)
            if not name or any(ch in name for ch in '",\n'):
                continue  # CSV/geocode-hostile name — keep frames resolvable
            pool.append({
                "name": name,
                "lat": float(lat),
                "lon": float(lon),
                "osm_id": e.osm_id,
                "osm_type": e.osm_type,
                "tags": e.tags or {},
            })
        return pool

    def _amenity_pool(self, country_code, snapshot_id) -> list:
        """[(amenity_value, count)] present in country ∩ parser vocab.

        Ordered with common signals first (keeps generated questions
        lexically consistent with the parser's training signal), then by
        descending entity count.
        """
        from django.db.models import Count
        vals = (
            OsmEntity.objects.using("vectors")
            .filter(snapshot_id=snapshot_id,
                    country_code=country_code,
                    tags__amenity__isnull=False)
            .exclude(geom__isnull=True)
            .values("tags__amenity")
            .annotate(n=Count("osm_id"))
            .order_by("-n")
        )
        vocab = self._amenity_vocab()
        pool = []
        try:
            for v in vals:
                amenity = v["tags__amenity"]
                if amenity and amenity in vocab:
                    pool.append((amenity, v["n"]))
        except Exception as exc:
            logger.warning("_amenity_pool failed for %s/%s: %s",
                           country_code, snapshot_id, exc)
            return []
        pool.sort(key=lambda t: (t[0] not in COMMON_AMENITIES, -t[1]))
        return pool

    def _amenities_near(self, anchor, radius_m, country_code, snapshot_id,
                        named_only: bool = False, limit: int = 20) -> list:
        """[(amenity, count)] grouped by amenity within ``radius_m`` of anchor.

        ``named_only=True`` restricts to named entities (answers are entity
        names); ``named_only=False`` counts every entity (the #1 answer is a
        plain count, matching the executor's unfiltered ST_DWithin count).
        Filtered to the parser's amenity vocabulary, ordered by count desc.
        """
        name_filter = " AND tags->>'name' IS NOT NULL" if named_only else ""
        with connections["vectors"].cursor() as cur:
            cur.execute(
                f"""
                SELECT tags->>'amenity' AS amenity, COUNT(*) AS n
                FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags->>'amenity' IS NOT NULL
                  AND geom IS NOT NULL{name_filter}
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint(%s, %s)::geography, %s)
                GROUP BY 1 ORDER BY n DESC LIMIT %s
                """,
                [snapshot_id, country_code, anchor["lon"], anchor["lat"],
                 float(radius_m), limit],
            )
            rows = cur.fetchall()
        vocab = self._amenity_vocab()
        return [(a, int(n)) for a, n in rows if a and a in vocab]

    def _named_amenities_near(self, anchor, radius_m, country_code,
                              snapshot_id, limit: int = 200) -> list:
        """Named amenity entities within ``radius_m`` of anchor (excl. self).

        Returns dicts {amenity, name, lat, lon, distance_m} ordered by
        distance — the shared pool for #4b/#5b/#8b anchor-relative sampling.
        """
        with connections["vectors"].cursor() as cur:
            cur.execute(
                """
                SELECT tags->>'amenity' AS amenity, tags->>'name' AS name,
                       ST_Y(geom) AS lat, ST_X(geom) AS lon,
                       ST_Distance(geom::geography,
                                   ST_MakePoint(%s, %s)::geography) AS d
                FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags->>'amenity' IS NOT NULL AND tags->>'name' IS NOT NULL
                  AND geom IS NOT NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint(%s, %s)::geography, %s)
                  AND NOT (osm_type = %s AND osm_id = %s)
                ORDER BY d ASC LIMIT %s
                """,
                [anchor["lon"], anchor["lat"], snapshot_id, country_code,
                 anchor["lon"], anchor["lat"], float(radius_m),
                 anchor["osm_type"], anchor["osm_id"], limit],
            )
            rows = cur.fetchall()
        vocab = self._amenity_vocab()
        out = []
        for amenity, name, lat, lon, d in rows:
            if not amenity or amenity not in vocab or not name:
                continue
            if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
                continue
            out.append({
                "amenity": amenity,
                "name": name,
                "lat": float(lat),
                "lon": float(lon),
                "distance_m": float(d),
            })
        return out

    def _amenity_vocab(self) -> set:
        """Parser amenity vocabulary (artifacts → training CSV → fallback)."""
        if self._vocab is not None:
            return self._vocab
        vocab = set()
        data_dir = Path(settings.MAPQA_PARSER_DATA_DIR) if getattr(
            settings, "MAPQA_PARSER_DATA_DIR", None) else None
        if data_dir:
            artifact = data_dir / "artifacts" / "amenity_vocab.json"
            if artifact.exists():
                try:
                    vocab.update(json.loads(artifact.read_text(encoding="utf-8")))
                except Exception as exc:
                    logger.warning("Failed to load amenity vocab artifact: %s", exc)
            if not vocab:
                csv_path = data_dir / "training_data" / "amenities.csv"
                if csv_path.exists():
                    for line in csv_path.read_text(encoding="utf-8").splitlines():
                        val = line.strip().strip('"').strip()
                        if val and val.lower() not in ("amenity", "null"):
                            vocab.add(val)
        vocab.update(COMMON_AMENITIES)
        self._vocab = vocab
        return vocab

    # ── Ground truth (PostGIS) ─────────────────────────────────────────────

    def _count_within(self, anchor, amenity, radius_m, country_code,
                      snapshot_id) -> int:
        with connections["vectors"].cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags->>'amenity' = %s
                  AND geom IS NOT NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint(%s, %s)::geography, %s)
                """,
                [snapshot_id, country_code, amenity,
                 anchor["lon"], anchor["lat"], float(radius_m)],
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def _nearest_amenity(self, anchor, amenity, country_code, snapshot_id,
                         max_radius_m=None):
        """Nearest named entity of ``amenity`` type to ``anchor`` (excl. self).

        Returns the entity's name or None (empty ground truth → row skipped).
        """
        params = [snapshot_id, country_code, amenity,
                  anchor["osm_type"], anchor["osm_id"]]
        sql = """
            SELECT tags->>'name' AS name
            FROM semantic_search_osmentity
            WHERE snapshot_id = %s AND country_code = %s
              AND tags->>'amenity' = %s
              AND geom IS NOT NULL AND tags->>'name' IS NOT NULL
              AND NOT (osm_type = %s AND osm_id = %s)
        """
        if max_radius_m is not None:
            sql += (
                " AND ST_DWithin(geom::geography,"
                " ST_MakePoint(%s, %s)::geography, %s)"
            )
            params += [anchor["lon"], anchor["lat"], float(max_radius_m)]
        sql += """
            ORDER BY ST_Distance(geom::geography,
                                 ST_MakePoint(%s, %s)::geography) ASC
            LIMIT 1
        """
        params += [anchor["lon"], anchor["lat"]]
        with connections["vectors"].cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        return row[0]

    def _nearest_in_direction(self, anchor, amenity, direction_deg,
                              country_code, snapshot_id,
                              radius_m=DIRECTION_NEAREST_RADIUS_M):
        """Nearest named entity of ``amenity`` type within ±45° of the
        cardinal axis through ``anchor`` (excl. self)."""
        with connections["vectors"].cursor() as cur:
            cur.execute(
                """
                SELECT tags->>'name' AS name,
                       ST_Y(geom) AS lat, ST_X(geom) AS lon,
                       ST_Distance(geom::geography,
                                   ST_MakePoint(%s, %s)::geography) AS d
                FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags->>'amenity' = %s
                  AND geom IS NOT NULL AND tags->>'name' IS NOT NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint(%s, %s)::geography, %s)
                  AND NOT (osm_type = %s AND osm_id = %s)
                """,
                [anchor["lon"], anchor["lat"], snapshot_id, country_code,
                 amenity, anchor["lon"], anchor["lat"], float(radius_m),
                 anchor["osm_type"], anchor["osm_id"]],
            )
            rows = cur.fetchall()
        best, best_dist = None, None
        for name, lat, lon, d in rows:
            if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
                continue
            bearing = self.bearing_degrees(anchor["lat"], anchor["lon"],
                                           lat, lon)
            diff = abs((bearing - direction_deg + 180) % 360 - 180)
            if diff <= DIRECTION_CONE_DEGREES:
                if best_dist is None or d < best_dist:
                    best, best_dist = name, d
        return best

    # ── Row helpers ─────────────────────────────────────────────────────────

    def _to_csv_row(self, row, country_code, snapshot_id,
                    pipeline_run_id) -> dict:
        template = row["template"]
        transform, metric = TEMPLATE_META[template]
        return {
            "Macro-template": template,
            "Concept transformation": transform,
            "What metric it produces": metric,
            "MapQA question": row["question"],
            "Question type": QUESTION_TYPES[template],
            "Region": "self_supervised",
            "Answer": row["answer"],
            "Source": "self_supervised",
            "Pipeline_run_id": pipeline_run_id or "",
            "Snapshot_date": snapshot_id,
            "Ground_truth_table": "semantic_search_osmentity",
            "Country_code": country_code,
        }

    @staticmethod
    def normalize_question(question: str) -> str:
        """Normalize for dedupe: lowercase, strip punctuation, collapse ws."""
        q = question.lower().strip().rstrip("?.").strip()
        q = re.sub(r"[^\w\s]", "", q)
        return re.sub(r"\s+", " ", q).strip()

    @staticmethod
    def _same_entity(a, b) -> bool:
        return a["osm_id"] == b["osm_id"] and a["osm_type"] == b["osm_type"]

    # ── Geo helpers (match the executor's formulas) ─────────────────────────

    @staticmethod
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine distance in km (identical to QueryExecutorService)."""
        R = 6371  # Earth's mean radius in km
        phi1, lam1 = math.radians(lat1), math.radians(lon1)
        phi2, lam2 = math.radians(lat2), math.radians(lon2)
        dphi = phi2 - phi1
        dlam = lam2 - lam1
        h = (math.sin(dphi / 2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
        return 2 * R * math.atan2(math.sqrt(h), math.sqrt(1 - h))

    @staticmethod
    def bearing_degrees(lat1: float, lon1: float,
                        lat2: float, lon2: float) -> float:
        """Initial bearing from point 1 to point 2 (executor's Eq. 8)."""
        phi1, lam1 = math.radians(lat1), math.radians(lon1)
        phi2, lam2 = math.radians(lat2), math.radians(lon2)
        dlam = lam2 - lam1
        y = math.sin(dlam) * math.cos(phi2)
        x = (math.cos(phi1) * math.sin(phi2) -
             math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
        return (math.degrees(math.atan2(y, x)) + 360) % 360

    @classmethod
    def cardinal_from_bearing(cls, theta: float) -> str:
        """8-way cardinal word for a bearing in degrees (0 = north)."""
        return CARDINALS[round(theta / 45) % 8]

    @staticmethod
    def _angular_diff(angle_a: float, angle_b: float) -> float:
        """Smallest absolute difference between two angles in degrees."""
        return abs((angle_a - angle_b + 180) % 360 - 180)
