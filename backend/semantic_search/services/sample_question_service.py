"""sample_question_service.py — the OSM RAG panel's sample-question bank.

Two scopes, both served through one service:

- **Country scope** — curated rows seeded from the CSV manifest
  (``load_sample_questions``, ``source='curated'``).
- **Subdivision scope** — GENERATED rows from the entities whose ``geom``
  falls inside the subdivision polygon (``geom__within`` via
  ``resolve_subdivision_polygon`` — the same geometry pattern the
  bbox-scoped search path uses in ``worldkg_nca/views/search.py``), so
  each question reflects operations *within* the subdivision and runs
  against the fast subdivision search.

The service only does two things — select the right entities (geometry
queries on the existing ``geom`` column) and pick the appropriate question
(the existing MapQA ``FRAMES`` + ``TEMPLATE_*`` constants). No template
machinery is modified.
"""

import logging

from django.db import connections
from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from semantic_search.models import SampleQuestion
from semantic_search.services.mapqa_samplers.constants import (
    FRAMES,
    TEMPLATE_FILTER_AGGREGATE,
    TEMPLATE_GEOCODE_BATCH,
    TEMPLATE_LOCATION_BEARING,
    TEMPLATE_OBJECT_FIELD,
    TEMPLATE_PLACE_ATTRIBUTE,
)
from semantic_search.utils.subdivision_resolver import (
    resolve_subdivision_polygon,
)

logger = logging.getLogger(__name__)

# Radii for generated subdivision questions: venue-scale (200-500m) and
# city-scale (1-5km) — the existing MapQA RADII_M stops at 500m, but a
# parish-level question needs kilometres to be answerable.
SUBDIVISION_RADII_M = (200, 500, 1000, 2000, 5000)

# #1 frame (FILTER-AGGREGATE-MEASURE) — "{amenity} within {r}m of {name}".
_FILTER_FRAME = FRAMES[TEMPLATE_FILTER_AGGREGATE][1]
# #2 frame (OBJECT-FIELD-MEASURE) — "How far is {a} from {b}?".
_DISTANCE_FRAME = FRAMES[TEMPLATE_OBJECT_FIELD][0]
# #4 nearest + #5 bearing + #8 attribute frames — the subdivision bank
# covers ALL 5 trained templates (criteria 2026-10-01).
_NEAREST_FRAME = FRAMES[TEMPLATE_GEOCODE_BATCH][2]
_BEARING_FRAME = FRAMES[TEMPLATE_LOCATION_BEARING][1]
# The proven #8 form (sample_questions.csv "What amenities are around
# Caye Caulker?" — the FRAMES library lacks an "around" variant).
_ATTRIBUTE_FRAME = "What amenities are around {name}?"

# One question per trained template, in this order (criteria 2026-10-01).
# The >= 3 results floor rides the question FORM, not the template: a
# class-LIST answer ("amenities or shop" — radius, attribute, nearest,
# cone-bearing) must verify with MIN_CLASS_RESULTS; a structural answer
# (a distance, a compare winner, a direction) is 1-2 results BY DESIGN
# and verifies at >= 1. Each builder stamps ``min_results`` on its rows.
SUBDIVISION_TEMPLATE_ORDER = (
    TEMPLATE_FILTER_AGGREGATE, TEMPLATE_OBJECT_FIELD,
    TEMPLATE_GEOCODE_BATCH, TEMPLATE_LOCATION_BEARING,
    TEMPLATE_PLACE_ATTRIBUTE,
)
MIN_CLASS_RESULTS = 3

# Raw OSM amenity values → natural plural nouns for the question text
# ("place_of_worship" → "churches" — the executor still resolves the
# canonical value either way).
_AMENITY_PLURALS = {
    "place_of_worship": "churches", "restaurant": "restaurants",
    "cafe": "cafes", "school": "schools", "bank": "banks",
    "pharmacy": "pharmacies", "hospital": "hospitals", "fuel": "fuel stations",
    "parking": "parking lots", "pub": "pubs", "bar": "bars",
    "library": "libraries", "cinema": "cinemas", "theatre": "theatres",
    "kindergarten": "kindergartens", "clinic": "clinics",
    "fast_food": "fast-food places", "marketplace": "markets",
    "bus_station": "bus stations", "police": "police stations",
    "post_office": "post offices", "atm": "ATMs", "charging_station":
    "charging stations", "community_centre": "community centres",
}


def _amenity_label(amenity: str) -> str:
    """Natural plural for a raw amenity value, else the value itself."""
    return _AMENITY_PLURALS.get(amenity, amenity)


# Question-generation recipes (2026-10-01): scope → templates → entity
# selection → verification. Every recipe ends in execute-and-keep: a
# question is stored only when the real parser+executor returns results
# for it (criterion: each question has results).
QUESTION_RECIPES = {
    "subdivision": {
        "scope": "subdivision",
        "templates": (
            "#1 FILTER-AGGREGATE-MEASURE", "#2 OBJECT-FIELD-MEASURE",
            "#4 GEOCODE-BATCH-COMPARE", "#5 LOCATION-BEARING-CLASSIFY",
            "#8 PLACE-ATTRIBUTE-QUERY",
        ),
        "entity_selection": "named entities geom__within the subdivision polygon; "
                            "amenities ST_DWithin bounded inside",
        "verification": (
            "execute-and-keep, ONE question per trained template; "
            "class-census templates verify with >= 3 results, the #2 "
            "distance answer with >= 1 (criteria 2026-10-01)",
        ),
    },
    "cross_subdivision": {
        "scope": "country (spans subdivisions)",
        "templates": ("#2 OBJECT-FIELD-MEASURE", "#4 GEOCODE-BATCH-COMPARE",
                      "#5 LOCATION-BEARING-CLASSIFY"),
        "entity_selection": "anchors from DIFFERENT subdivisions of the country",
        "verification": "execute-and-keep",
    },
}


class SampleQuestionService:
    """Query and generate the sample-question bank."""

    # ── Serving ───────────────────────────────────────────────────────────

    @classmethod
    def list_questions(cls, country_code: str, subdivision_qid: str = None,
                       snapshot_date: str = None) -> dict:
        """Questions for a country, or for a subdivision (falling back to
        the country's curated set when the subdivision has none
        generated). Returns ``{"scope": "country"|"subdivision",
        "questions": [...]}``."""
        country_code = (country_code or "").upper()
        qid = subdivision_qid or ""
        if qid:
            rows = list(SampleQuestion.objects.filter(
                country_code=country_code, subdivision_qid=qid,
            ).order_by("question"))
            if rows:
                return {
                    "scope": "subdivision",
                    "subdivision_qid": qid,
                    "questions": [cls._row(r) for r in rows],
                }
        rows = list(SampleQuestion.objects.filter(
            country_code=country_code, subdivision_qid="",
        ).order_by("question"))
        return {
            "scope": "country",
            "questions": [cls._row(r) for r in rows],
        }

    @staticmethod
    def _row(r: SampleQuestion) -> dict:
        return {
            "question": r.question,
            "template": r.template,
            "source": r.source,
            "anchor": r.anchor,
        }

    # ── Entity selection (geometry — the existing pattern) ───────────────

    @classmethod
    def _named_entities_in(cls, country_code: str, snapshot_id: str,
                           polygon, limit: int) -> list:
        """Named entities whose geom is inside the subdivision polygon.

        ``geom__within`` on the bbox polygon — the same GeoDjango pattern
        the search path uses. Deterministic: ordered by osm_id.
        """
        from worldkg_nca.models import OsmEntity

        qs = (
            OsmEntity.objects.using("vectors")
            .filter(snapshot_id=snapshot_id,
                    country_code=country_code,
                    tags__name__isnull=False)
            .exclude(geom__isnull=True)
            .filter(geom__within=polygon)
            .annotate(
                lat=RawSQL("ST_Y(geom)", [], output_field=FloatField()),
                lon=RawSQL("ST_X(geom)", [], output_field=FloatField()),
            )
            .order_by("osm_id")[:limit]
        )
        return [
            {
                "name": (e.tags or {}).get("name", ""),
                "osm_id": e.osm_id,
                "lat": e.lat,
                "lon": e.lon,
            }
            for e in qs
            if (e.tags or {}).get("name")
        ]

    @classmethod
    def _amenities_near(cls, anchor: dict, radius_m: int, country_code: str,
                        snapshot_id: str, polygon=None) -> list:
        """[(amenity, count)] grouped by amenity within radius of the
        anchor, bounded INSIDE the subdivision polygon when one is given
        (operations within the subdivision); ``polygon=None`` is the
        country-wide census (the cross-subdivision recipe). Mirrors the
        MapQA generator's ST_DWithin count."""
        polygon_clause = (
            " AND ST_Within(geom, ST_GeomFromText(%s, 4326))"
            if polygon is not None else ""
        )
        params = [snapshot_id, country_code.upper(),
                  anchor["lon"], anchor["lat"], float(radius_m)]
        if polygon is not None:
            params.append(polygon.wkt)
        with connections["vectors"].cursor() as cur:
            cur.execute(
                """
                SELECT tags->>'amenity' AS amenity, count(*) AS n
                FROM semantic_search_osmentity
                WHERE snapshot_id = %s AND country_code = %s
                  AND tags ? 'amenity' AND geom IS NOT NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint(%s, %s)::geography, %s)
                """ + polygon_clause + """
                GROUP BY tags->>'amenity'
                ORDER BY n DESC
                LIMIT 8
                """,
                params,
            )
            return [(r[0], r[1]) for r in cur.fetchall() if r[0]]

    @staticmethod
    def _country_snapshot(country_code: str) -> str:
        """The newest snapshot that actually has entities for the country.

        The country profile's ``snapshot_date`` is preferred when it
        matches the vectors data, else the newest snapshot present —
        profile dates can lag the data (BZ profile says 2026_09_10 while
        the entities live under 2025_12_31, observed 2026-10-01).
        """
        from django.db import connections
        from worldkg_nca.snapshot_utils import get_latest_snapshot_id

        cc = (country_code or "").upper()
        with connections["vectors"].cursor() as cur:
            cur.execute(
                "SELECT snapshot_id FROM semantic_search_osmentity "
                "WHERE country_code = %s GROUP BY snapshot_id "
                "ORDER BY snapshot_id DESC",
                [cc],
            )
            present = [r[0] for r in cur.fetchall()]
        if not present:
            return get_latest_snapshot_id()
        from core.models import CountryPipelineProfile
        profile = CountryPipelineProfile.objects.filter(
            iso2__iexact=cc,
        ).first()
        if profile and profile.snapshot_date in present:
            return profile.snapshot_date
        return present[0]

    # ── Generation ───────────────────────────────────────────────────────

    @classmethod
    def _select_one_per_template(cls, rows: list, country_code: str,
                                 snapshot_id: str) -> tuple:
        """Verify candidate rows and pick ONE question per trained template.

        Each candidate stamps its own ``min_results`` floor (3 for a
        class-LIST form, 1 for a structural distance/compare/direction
        answer). Returns (selected, verified, dropped) — criteria
        2026-10-01, shared by the subdivision and cross-subdivision
        recipes.
        """
        selected = []
        verified = dropped = 0
        for template in SUBDIVISION_TEMPLATE_ORDER:
            for cand in rows:
                if cand["template"] != template:
                    continue
                check = cls._verify_question(
                    cand["question"], country_code, snapshot_id,
                    min_results=cand.get("min_results", 1),
                )
                if check["ok"]:
                    selected.append(cand)
                    verified += 1
                    break
                dropped += 1
        return selected, verified, dropped

    @classmethod
    def generate_subdivision(cls, country_code: str, subdivision_qid: str,
                             snapshot_date: str = None,
                             pool_size: int = 60,
                             per_anchor: int = 2,
                             min_entities: int = 2) -> dict:
        """Deterministic generation of subdivision-scoped questions.

        Selects named entities inside the subdivision polygon, then for a
        sample of anchors builds candidates covering ALL 5 trained
        templates: #1 radius + #4 nearest + #5 bearing + #8 attribute
        from the amenities that actually exist near the anchor (inside
        the polygon), and #2 distance between adjacent anchors. Selection
        keeps ONE question per template, first verified candidate in
        template order; class-census templates must verify with >= 3
        results, the #2 distance answer with >= 1 (criteria 2026-10-01).
        Stores rows (``source='generated'``), replacing prior generated
        rows for the subdivision. Subdivisions with fewer than
        ``min_entities`` named entities are SKIPPED
        (``{"skipped": True}``) — the country fallback serves them.
        """
        country_code = (country_code or "").upper()
        polygon = resolve_subdivision_polygon(subdivision_qid)
        if polygon is None:
            return {
                "error": f"no bbox populated for subdivision {subdivision_qid}",
            }
        # resolve_subdivision_polygon returns an SRID-0 Polygon; the
        # geom__within filter against SRID-4326 entity geoms requires the
        # SRID set (mixed-SRID error observed 2026-09-30).
        polygon.srid = 4326
        snapshot_id = snapshot_date or cls._country_snapshot(country_code)
        entities = cls._named_entities_in(
            country_code, snapshot_id, polygon, pool_size,
        )
        if len(entities) < min_entities:
            return {
                "skipped": True,
                "generated": 0,
                "reason": (
                    f"only {len(entities)} named entities inside "
                    f"{subdivision_qid} (min_entities={min_entities})"
                ),
            }

        # Candidate rows covering ALL 5 trained templates (criteria
        # 2026-10-01): #1 radius, #4 nearest, #5 bearing, and #8 attribute
        # from each anchor's amenity census; #2 distance between adjacent
        # anchors.
        rows = []
        step = max(1, len(entities) // 8)
        for anchor in entities[::step]:
            if len(rows) >= pool_size * 3:
                break
            nearby = None
            for radius_m in SUBDIVISION_RADII_M:
                nearby = cls._amenities_near(
                    anchor, radius_m, country_code, snapshot_id, polygon,
                )
                if nearby:
                    break
            if not nearby:
                continue
            amenity, _count = nearby[0]
            label = _amenity_label(amenity)
            rows.append({
                "question": _FILTER_FRAME.format(
                    amenity=label, name=anchor["name"], r=radius_m,
                ),
                "template": TEMPLATE_FILTER_AGGREGATE,
                "anchor": anchor["name"],
                "min_results": MIN_CLASS_RESULTS,
            })
            rows.append({
                "question": _NEAREST_FRAME.format(
                    amenity=label, name=anchor["name"],
                ),
                "template": TEMPLATE_GEOCODE_BATCH,
                "anchor": anchor["name"],
                "min_results": MIN_CLASS_RESULTS,
            })
            rows.append({
                "question": _ATTRIBUTE_FRAME.format(name=anchor["name"]),
                "template": TEMPLATE_PLACE_ATTRIBUTE,
                "anchor": anchor["name"],
                "min_results": MIN_CLASS_RESULTS,
            })
            for direction in ("north", "east", "south", "west"):
                rows.append({
                    "question": _BEARING_FRAME.format(
                        amenity=label, dir=direction, name=anchor["name"],
                    ),
                    "template": TEMPLATE_LOCATION_BEARING,
                    "anchor": anchor["name"],
                    "min_results": MIN_CLASS_RESULTS,
                })
        # #2 distance questions: adjacent anchors in the deterministic
        # pool order (both inside the subdivision). Skip identical-name
        # pairs ("How far is San Pablo from San Pablo?" — same anchor name).
        for i in range(0, min(len(entities) - 1, 8), 1):
            a, b = entities[i], entities[i + 1]
            if a["name"].strip().lower() == b["name"].strip().lower():
                continue
            rows.append({
                "question": _DISTANCE_FRAME.format(a=a["name"], b=b["name"]),
                "template": TEMPLATE_OBJECT_FIELD,
                "anchor": a["name"],
                "min_results": 1,  # a distance is one number by design
            })

        # Selection: ONE question per trained template, first verified
        # candidate in template order (each row stamps its own floor).
        selected, verified, dropped = cls._select_one_per_template(
            rows, country_code, snapshot_id,
        )

        if not selected:
            return {
                "error": (
                    "no questions generated (no template verified with "
                    f"the minimum {MIN_CLASS_RESULTS} results)"
                ),
                "generated": 0, "verified": verified,
                "dropped": dropped, "snapshot_date": snapshot_id,
            }

        stored, _, _ = cls._store_rows(
            country_code, subdivision_qid, selected, snapshot_id,
            verify=False,  # selection already verified
        )
        logger.info(
            "Generated %d (one per %d templates; verified %d, dropped %d "
            "candidates) sample questions for %s/%s",
            len(stored), len(SUBDIVISION_TEMPLATE_ORDER), verified,
            dropped, country_code, subdivision_qid,
        )
        return {
            "generated": len(stored), "verified": verified,
            "dropped": dropped, "snapshot_date": snapshot_id,
        }

    # ── Verification (execute-and-keep — criterion: every question has results)

    @staticmethod
    def _verify_question(question: str, country_code: str,
                         snapshot_date: str, min_results: int = 1) -> dict:
        """Run a question through the REAL parser + executor (deterministic,
        skip_enrichment) — a question is kept only when it returns at least
        ``min_results`` rows.

        This is the enforcement for "each question should have results":
        construction (DB counts) cannot catch executor-side resolution
        differences (amenity-tag mapping, geocoding of #2 anchors, noise
        filters); execution does. The subdivision generator raises the
        floor to 3 for class-census questions (2026-10-01) so a sample
        with a single entity is not stored.
        """
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )
        try:
            parsed = QueryParserService.get_instance().parse(question)
            if not (parsed or {}).get("template"):
                return {"ok": False, "result_count": 0,
                        "error": "parser returned no template"}
            result = QueryExecutorService.execute(
                parsed, country_code, snapshot_date,
                question=question, skip_enrichment=True,
            )
            if result.get("error"):
                return {"ok": False, "result_count": 0,
                        "error": result["error"]}
            rows = result.get("results") or []
            return {
                "ok": len(rows) >= min_results,
                "result_count": len(rows) if isinstance(rows, list) else 0,
            }
        except Exception as exc:  # noqa: BLE001 — verification must never raise
            return {"ok": False, "result_count": 0, "error": str(exc)}

    @classmethod
    def _store_rows(cls, country_code: str, subdivision_qid: str, rows: list,
                    snapshot_id: str, verify: bool = True) -> tuple:
        """Dedupe → verify (execute-and-keep) → replace prior generated rows.

        Returns (stored, verified, dropped). Dedupe by question first: two
        anchors can share a name ("San Pablo" twice) and the unique
        constraint would reject the batch (observed 2026-10-01).
        """
        seen = set()
        unique = []
        for r in rows:
            if r["question"] in seen:
                continue
            seen.add(r["question"])
            unique.append(r)
        verified, dropped = 0, 0
        if verify:
            kept = []
            for r in unique:
                check = cls._verify_question(
                    r["question"], country_code, snapshot_id,
                )
                if check["ok"]:
                    kept.append(r)
                    verified += 1
                else:
                    dropped += 1
            unique = kept
        SampleQuestion.objects.filter(
            country_code=country_code,
            subdivision_qid=subdivision_qid,
            source="generated",
        ).delete()
        SampleQuestion.objects.bulk_create([
            SampleQuestion(
                country_code=country_code,
                subdivision_qid=subdivision_qid,
                question=r["question"],
                template=r["template"],
                anchor=r["anchor"],
                source="generated",
                snapshot_date=snapshot_id or "",
            )
            for r in unique
        ])
        return unique, verified, dropped

    # ── Cross-subdivision recipe (country-wide questions spanning
    #    subdivisions) ────────────────────────────────────────────────────

    @classmethod
    def generate_cross_subdivision(cls, country_code: str,
                                   snapshot_date: str = None,
                                   max_pairs: int = 8) -> dict:
        """Country-wide questions that span DIFFERENT subdivisions.

        Recipe ``cross_subdivision``: ONE question per trained template
        (criteria 2026-10-01) — #1 radius + #8 attribute from a hub
        anchor's country-wide amenity census, #2 distance / #5 bearing /
        #4 compare between anchors from different subdivisions. Class-
        census templates verify with >= 3 results, the #2 distance answer
        with >= 1. Stored as country-level generated rows
        (``subdivision_qid=''``), joined with the curated bank.
        """
        from django.db.models import Q as _Q

        from core.models import SubgraphProfile

        country_code = (country_code or "").upper()
        snapshot_id = snapshot_date or cls._country_snapshot(country_code)
        subs = list(SubgraphProfile.objects.filter(
            country_profile__iso2__iexact=country_code,
            wikidata_id__isnull=False,
        ).filter(
            _Q(bbox_min_lon__isnull=False) & _Q(bbox_max_lat__isnull=False),
        ).order_by("name"))
        if len(subs) < 2:
            return {
                "skipped": True, "generated": 0,
                "reason": f"fewer than 2 subdivisions ({len(subs)})",
            }

        rows = []
        step = max(1, len(subs) // max_pairs)
        for i in range(0, min(len(subs), max_pairs * step), step):
            a = subs[i]
            b = subs[(i + 1) % len(subs)]
            if a.wikidata_id == b.wikidata_id:
                continue
            anchor_a = cls._first_named_entity(
                country_code, snapshot_id, a.wikidata_id,
            )
            anchor_b = cls._first_named_entity(
                country_code, snapshot_id, b.wikidata_id,
            )
            if not anchor_a or not anchor_b:
                continue
            # #1 radius + #8 attribute from the first anchor's country-wide
            # amenity census (polygon=None — country scale, no district
            # bound; radii sized to span subdivisions).
            if not any(r["template"] == TEMPLATE_FILTER_AGGREGATE for r in rows):
                for radius_m in (10000, 5000):
                    nearby = cls._amenities_near(
                        anchor_a, radius_m, country_code, snapshot_id,
                        polygon=None,
                    )
                    if not nearby:
                        continue
                    amenity, _count = nearby[0]
                    label = _amenity_label(amenity)
                    rows.append({
                        "question": _FILTER_FRAME.format(
                            amenity=label, name=anchor_a["name"], r=radius_m,
                        ),
                        "template": TEMPLATE_FILTER_AGGREGATE,
                        "anchor": anchor_a["name"],
                        "min_results": MIN_CLASS_RESULTS,
                    })
                    rows.append({
                        "question": _ATTRIBUTE_FRAME.format(
                            name=anchor_a["name"],
                        ),
                        "template": TEMPLATE_PLACE_ATTRIBUTE,
                        "anchor": anchor_a["name"],
                        "min_results": MIN_CLASS_RESULTS,
                    })
                    break
            # #2 — distance across subdivisions (structural: 1 answer).
            rows.append({
                "question": _DISTANCE_FRAME.format(
                    a=anchor_a["name"], b=anchor_b["name"],
                ),
                "template": TEMPLATE_OBJECT_FIELD,
                "anchor": anchor_a["name"],
                "min_results": 1,
            })
            # #5 — bearing across subdivisions (structural: 1 direction).
            rows.append({
                "question": FRAMES[TEMPLATE_LOCATION_BEARING][0].format(
                    x=anchor_b["name"], y=anchor_a["name"],
                ),
                "template": TEMPLATE_LOCATION_BEARING,
                "anchor": anchor_a["name"],
                "min_results": 1,
            })
            # #4 — compare across three subdivisions (structural: 1-2
            # answers by design).
            c = subs[(i + 2) % len(subs)]
            if c.wikidata_id not in (a.wikidata_id, b.wikidata_id):
                anchor_c = cls._first_named_entity(
                    country_code, snapshot_id, c.wikidata_id,
                )
                if anchor_c:
                    rows.append({
                        "question": FRAMES[TEMPLATE_GEOCODE_BATCH][0].format(
                            z=anchor_a["name"], x=anchor_b["name"],
                            y=anchor_c["name"],
                        ),
                        "template": TEMPLATE_GEOCODE_BATCH,
                        "anchor": anchor_a["name"],
                        "min_results": 1,
                    })
        if not rows:
            return {"error": "no cross-subdivision questions generated"}

        # Selection: ONE question per trained template, verified with the
        # template's floor (same criteria as the subdivision recipe).
        selected, verified, dropped = cls._select_one_per_template(
            rows, country_code, snapshot_id,
        )
        if not selected:
            return {
                "error": "no cross-subdivision questions verified",
                "generated": 0, "verified": verified,
                "dropped": dropped, "snapshot_date": snapshot_id,
            }

        stored, _, _ = cls._store_rows(
            country_code, "", selected, snapshot_id, verify=False,
        )
        logger.info(
            "Generated %d (one per %d templates; verified %d, dropped %d "
            "candidates) cross-subdivision questions for %s",
            len(stored), len(SUBDIVISION_TEMPLATE_ORDER), verified,
            dropped, country_code,
        )
        return {
            "generated": len(stored), "verified": verified,
            "dropped": dropped, "snapshot_date": snapshot_id,
        }

    @classmethod
    def _first_named_entity(cls, country_code: str, snapshot_id: str,
                            subdivision_qid: str):
        """The first named entity inside a subdivision (deterministic)."""
        polygon = resolve_subdivision_polygon(subdivision_qid)
        if polygon is None:
            return None
        polygon.srid = 4326
        entities = cls._named_entities_in(
            country_code, snapshot_id, polygon, limit=1,
        )
        return entities[0] if entities else None
