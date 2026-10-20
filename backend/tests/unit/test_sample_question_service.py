"""Unit tests for the sample-question service (scope serving + labels).

Geometry generation is DB-backed (live-verified against the vectors DB);
these tests cover the deterministic serving/label logic with a mocked
SampleQuestion manager.
"""

from unittest import mock

from semantic_search.models import SampleQuestion
from semantic_search.services.sample_question_service import (
    SampleQuestionService,
    _amenity_label,
)


def _row(question, template="FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
         source="curated", anchor="Belmopan"):
    r = mock.Mock(spec=SampleQuestion)
    r.question = question
    r.template = template
    r.source = source
    r.anchor = anchor
    return r


class TestListQuestions:
    def test_country_scope(self):
        rows = [_row("Which cafes are within 2km of Belmopan?")]
        with mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(filter=mock.Mock(
                return_value=mock.Mock(order_by=mock.Mock(return_value=rows)),
            )),
        ):
            result = SampleQuestionService.list_questions("bz")
        assert result["scope"] == "country"
        assert result["questions"][0]["question"].startswith("Which cafes")

    def test_subdivision_scope_when_generated(self):
        sub_rows = [_row("How far is Punta Gorda from Dolores?",
                         "OBJECT-FIELD-MEASURE (***REMOVED***2)", "generated", "Punta Gorda")]
        country_rows = [_row("Which cafes are within 2km of Belmopan?")]

        def fake_filter(**kwargs):
            if kwargs.get("subdivision_qid"):
                return mock.Mock(order_by=mock.Mock(return_value=sub_rows))
            return mock.Mock(order_by=mock.Mock(return_value=country_rows))

        with mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(filter=mock.Mock(side_effect=fake_filter)),
        ):
            result = SampleQuestionService.list_questions("BZ", "Q506049")
        assert result["scope"] == "subdivision"
        assert result["subdivision_qid"] == "Q506049"
        assert result["questions"][0]["source"] == "generated"

    def test_subdivision_falls_back_to_country(self):
        ***REMOVED*** No generated rows for the subdivision → country curated set.
        country_rows = [_row("Which cafes are within 2km of Belmopan?")]

        def fake_filter(**kwargs):
            if kwargs.get("subdivision_qid"):
                return mock.Mock(order_by=mock.Mock(return_value=[]))
            return mock.Mock(order_by=mock.Mock(return_value=country_rows))

        with mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(filter=mock.Mock(side_effect=fake_filter)),
        ):
            result = SampleQuestionService.list_questions("BZ", "Q999999")
        assert result["scope"] == "country"


class TestAmenityLabel:
    def test_natural_plurals(self):
        assert _amenity_label("place_of_worship") == "churches"
        assert _amenity_label("restaurant") == "restaurants"
        assert _amenity_label("fuel") == "fuel stations"

    def test_unknown_value_passes_through(self):
        assert _amenity_label("custom_thing") == "custom_thing"


class TestGenerateSubdivision:
    @staticmethod
    def _poly():
        p = mock.Mock()
        p.wkt = "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
        return p

    def test_sparse_subdivision_skipped(self):
        ***REMOVED*** Fewer named entities than min_entities → skip (country fallback
        ***REMOVED*** serves it), not an error. Prior generated rows are cleared so a
        ***REMOVED*** stale bank does not keep serving.
        delete = mock.Mock()
        with mock.patch(
            "semantic_search.services.sample_question_service.resolve_subdivision_polygon",
            return_value=self._poly(),
        ), mock.patch.object(
            SampleQuestionService, "_country_snapshot",
            return_value="2025_12_31",
        ), mock.patch.object(
            SampleQuestionService, "_named_entities_in", return_value=[],
        ), mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(filter=mock.Mock(return_value=mock.Mock(delete=delete))),
        ):
            result = SampleQuestionService.generate_subdivision(
                "BZ", "Q999999", min_entities=2,
            )
        assert result["skipped"] is True
        assert result["generated"] == 0
        delete.assert_called_once()

    def test_duplicate_anchor_names_deduped(self):
        ***REMOVED*** Two anchors with the same name produce the same question — the
        ***REMOVED*** unique constraint must not reject the batch.
        anchors = [
            {"name": "San Pablo", "osm_id": 1, "lat": 1.0, "lon": 1.0},
            {"name": "San Pablo", "osm_id": 2, "lat": 1.1, "lon": 1.1},
        ]
        created = []

        def fake_create(*args, **kwargs):
            created.extend(args[0] if args else kwargs.get("objs", []))
            return []

        with mock.patch(
            "semantic_search.services.sample_question_service.resolve_subdivision_polygon",
            return_value=self._poly(),
        ), mock.patch.object(
            SampleQuestionService, "_country_snapshot",
            return_value="2025_12_31",
        ), mock.patch.object(
            SampleQuestionService, "_named_entities_in", return_value=anchors,
        ), mock.patch.object(
            SampleQuestionService, "_amenities_near",
            return_value=[("place_of_worship", 3)],
        ), mock.patch.object(
            SampleQuestionService, "_amenity_entity_count",
            return_value=3,
        ), mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(
                filter=mock.Mock(return_value=mock.Mock(delete=mock.Mock())),
                bulk_create=mock.Mock(side_effect=fake_create),
            ),
        ):
            result = SampleQuestionService.generate_subdivision(
                "BZ", "Q512273",
            )
        questions = {getattr(o, "question") for o in created}
        assert result["generated"] == len(created)
        assert len(questions) == len(created)  ***REMOVED*** no duplicates persisted
        assert "Which churches are within 200m of San Pablo?" in questions

    def test_verify_drops_questions_without_results(self):
        ***REMOVED*** DB-evidence keep: a candidate whose census evidence is below its
        ***REMOVED*** floor is not stored. The class-LIST forms need >= 3 entities of
        ***REMOVED*** the amenity in range; the ***REMOVED***2 distance answer (two real named
        ***REMOVED*** anchors in the pool) always keeps.
        anchors = [
            {"name": "San Pablo", "osm_id": 1, "lat": 1.0, "lon": 1.0},
            {"name": "San Juan", "osm_id": 2, "lat": 1.1, "lon": 1.1},
        ]
        created = []

        def fake_create(*args, **kwargs):
            created.extend(args[0] if args else kwargs.get("objs", []))
            return []

        with mock.patch(
            "semantic_search.services.sample_question_service.resolve_subdivision_polygon",
            return_value=self._poly(),
        ), mock.patch.object(
            SampleQuestionService, "_country_snapshot",
            return_value="2025_12_31",
        ), mock.patch.object(
            SampleQuestionService, "_named_entities_in", return_value=anchors,
        ), mock.patch.object(
            SampleQuestionService, "_amenities_near",
            return_value=[("place_of_worship", 1)],
        ), mock.patch.object(
            SampleQuestionService, "_amenity_entity_count",
            return_value=1,
        ), mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(
                filter=mock.Mock(return_value=mock.Mock(delete=mock.Mock())),
                bulk_create=mock.Mock(side_effect=fake_create),
            ),
        ):
            result = SampleQuestionService.generate_subdivision(
                "BZ", "Q512273",
            )
        stored = {getattr(o, "template") for o in created}
        assert result["generated"] == 1  ***REMOVED*** only the ***REMOVED***2 distance question
        assert result["dropped"] >= 1
        assert stored == {"OBJECT-FIELD-MEASURE (***REMOVED***2)"}

    def test_one_question_per_template_selected(self):
        ***REMOVED*** Criteria 2026-10-01: the subdivision bank is exactly ONE
        ***REMOVED*** question per trained template (***REMOVED***1 ***REMOVED***2 ***REMOVED***4 ***REMOVED***5 ***REMOVED***8).
        anchors = [
            {"name": "San Pablo", "osm_id": 1, "lat": 1.0, "lon": 1.0},
            {"name": "San Juan", "osm_id": 2, "lat": 1.1, "lon": 1.1},
        ]
        created = []

        def fake_create(*args, **kwargs):
            created.extend(args[0] if args else kwargs.get("objs", []))
            return []

        with mock.patch(
            "semantic_search.services.sample_question_service.resolve_subdivision_polygon",
            return_value=self._poly(),
        ), mock.patch.object(
            SampleQuestionService, "_country_snapshot",
            return_value="2025_12_31",
        ), mock.patch.object(
            SampleQuestionService, "_named_entities_in", return_value=anchors,
        ), mock.patch.object(
            SampleQuestionService, "_amenities_near",
            return_value=[("place_of_worship", 3)],
        ), mock.patch.object(
            SampleQuestionService, "_amenity_entity_count",
            return_value=3,
        ), mock.patch.object(
            SampleQuestion, "objects",
            mock.Mock(
                filter=mock.Mock(return_value=mock.Mock(delete=mock.Mock())),
                bulk_create=mock.Mock(side_effect=fake_create),
            ),
        ):
            result = SampleQuestionService.generate_subdivision(
                "BZ", "Q512273",
            )
        assert result["generated"] == 5
        templates = {getattr(o, "template") for o in created}
        assert templates == {
            "FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
            "OBJECT-FIELD-MEASURE (***REMOVED***2)",
            "GEOCODE-BATCH-COMPARE (***REMOVED***4)",
            "LOCATION-BEARING-CLASSIFY (***REMOVED***5)",
            "PLACE-ATTRIBUTE-QUERY (***REMOVED***8)",
        }
        assert result["dropped"] == 0

    def test_select_one_per_template_uses_candidate_floor(self):
        ***REMOVED*** The >= 3 floor rides the question FORM (a class list), not the
        ***REMOVED*** template: a class question whose DB evidence is below its floor
        ***REMOVED*** is dropped, while a structural answer (distance) keeps at >= 1
        ***REMOVED*** (2026-10-01; evidence-based since 2026-10-11).
        rows = [
            {"question": "Which churches are within 200m of San Pablo?",
             "template": "FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
             "anchor": "San Pablo", "min_results": 3,
             "evidence_count": 2},
            {"question": "How far is San Pablo from San Juan?",
             "template": "OBJECT-FIELD-MEASURE (***REMOVED***2)",
             "anchor": "San Pablo", "min_results": 1,
             "evidence_count": 2},
        ]
        selected, verified, dropped = (
            SampleQuestionService._select_one_per_template(rows)
        )
        assert [r["template"] for r in selected] == [
            "OBJECT-FIELD-MEASURE (***REMOVED***2)",
        ]
        assert verified == 1
        assert dropped == 1

    def test_country_snapshot_prefers_present(self):
        ***REMOVED*** The profile snapshot_date wins only when it matches the vectors
        ***REMOVED*** data; otherwise the newest present snapshot is used (BZ profile
        ***REMOVED*** said 2026_09_10, data under 2025_12_31).
        fake_cursor = mock.Mock()
        fake_cursor.fetchall.return_value = [
            ("2025_12_31",), ("2024_05_28",),
        ]

        class FakeConn:
            def cursor(self):
                return mock.MagicMock(
                    __enter__=mock.Mock(return_value=fake_cursor),
                    __exit__=mock.Mock(return_value=False),
                )

        conn = mock.MagicMock()
        conn.__getitem__.return_value = FakeConn()
        with mock.patch(
            "django.db.connections", conn,
        ), mock.patch(
            "core.models.CountryPipelineProfile.objects.filter",
            return_value=mock.Mock(first=mock.Mock(return_value=None)),
        ):
            snapshot = SampleQuestionService._country_snapshot("BZ")
        assert snapshot == "2025_12_31"
