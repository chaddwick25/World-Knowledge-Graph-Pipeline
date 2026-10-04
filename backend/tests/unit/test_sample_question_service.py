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


def _row(question, template="FILTER-AGGREGATE-MEASURE (#1)",
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
                         "OBJECT-FIELD-MEASURE (#2)", "generated", "Punta Gorda")]
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
        # No generated rows for the subdivision → country curated set.
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
        # Fewer named entities than min_entities → skip (country fallback
        # serves it), not an error. Prior generated rows are cleared so a
        # stale bank does not keep serving.
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
        # Two anchors with the same name produce the same question — the
        # unique constraint must not reject the batch.
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
            SampleQuestionService, "_verify_question",
            return_value={"ok": True, "result_count": 1},
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
        assert len(questions) == len(created)  # no duplicates persisted
        assert "Which churches are within 200m of San Pablo?" in questions

    def test_verify_drops_questions_without_results(self):
        # Execute-and-keep: a candidate the verifier rejects is not stored.
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
            SampleQuestionService, "_verify_question",
            return_value={"ok": False, "result_count": 0, "error": "none"},
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
        assert result["generated"] == 0
        assert result["dropped"] >= 1
        assert created == []

    def test_one_question_per_template_selected(self):
        # Criteria 2026-10-01: the subdivision bank is exactly ONE
        # question per trained template (#1 #2 #4 #5 #8).
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
            SampleQuestionService, "_verify_question",
            return_value={"ok": True, "result_count": 3},
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
            "FILTER-AGGREGATE-MEASURE (#1)",
            "OBJECT-FIELD-MEASURE (#2)",
            "GEOCODE-BATCH-COMPARE (#4)",
            "LOCATION-BEARING-CLASSIFY (#5)",
            "PLACE-ATTRIBUTE-QUERY (#8)",
        }
        assert result["dropped"] == 0

    def test_select_one_per_template_uses_candidate_floor(self):
        # The >= 3 floor rides the question FORM (a class list), not the
        # template: a class question with < 3 results is dropped, while a
        # structural answer (distance) verifies at >= 1 (2026-10-01).
        rows = [
            {"question": "Which churches are within 200m of San Pablo?",
             "template": "FILTER-AGGREGATE-MEASURE (#1)",
             "anchor": "San Pablo", "min_results": 3},
            {"question": "How far is San Pablo from San Juan?",
             "template": "OBJECT-FIELD-MEASURE (#2)",
             "anchor": "San Pablo", "min_results": 1},
        ]
        with mock.patch.object(
            SampleQuestionService, "_verify_question",
            side_effect=lambda q, cc, snap, min_results=1: {
                "ok": min_results <= 1, "result_count": 1,
            },
        ):
            selected, verified, dropped = (
                SampleQuestionService._select_one_per_template(
                    rows, "BZ", "2025_12_31",
                )
            )
        assert [r["template"] for r in selected] == [
            "OBJECT-FIELD-MEASURE (#2)",
        ]
        assert verified == 1
        assert dropped == 1

    def test_country_snapshot_prefers_present(self):
        # The profile snapshot_date wins only when it matches the vectors
        # data; otherwise the newest present snapshot is used (BZ profile
        # said 2026_09_10, data under 2025_12_31).
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
