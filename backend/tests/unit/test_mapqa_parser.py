"""Unit tests for the MapQA parser service.

Tests parser invariants:
  - Template classification accuracy on representative questions
  - Concept extraction (amenity vocab, radius, entity names)
  - Role precedence (G2 invariant)
  - DAG validation
  - Multi-entity extraction
  - Confidence is in [0, 1]
  - TF-IDF non-negativity (no negative features)

Run inside Docker:
  docker compose exec backend python -m pytest tests/unit/test_mapqa_parser.py -v
"""

import os
import pytest


@pytest.fixture(scope="module")
def parser():
    """Load the parser singleton once for all tests."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    import django
    django.setup()
    from semantic_search.services.query_parser_service import QueryParserService
    QueryParserService.reset_instance()
    return QueryParserService.get_instance()


# ── Template classification ──────────────────────────────────────────

class TestTemplateClassification:
    """Test that the 5 templates are correctly classified."""

    @pytest.mark.parametrize("question,expected_template", [
        ("Which bars are within 50m of Hollywood Blvd?", "FILTER-AGGREGATE-MEASURE (#1)"),
        ("How far is Union Station from downtown LA?", "OBJECT-FIELD-MEASURE (#2)"),
        ("What amenity is available at Union Station?", "PLACE-ATTRIBUTE-QUERY (#8)"),
        ("Which restaurant is nearest to Union Station?", "GEOCODE-BATCH-COMPARE (#4)"),
        ("What is west of Union Station?", "LOCATION-BEARING-CLASSIFY (#5)"),
    ])
    def test_template_classification(self, parser, question, expected_template):
        result = parser.parse(question)
        assert result["template"] == expected_template, (
            f"Expected {expected_template}, got {result['template']} "
            f"(conf={result['confidence']:.3f}) for: {question}"
        )

    def test_confidence_in_range(self, parser):
        """Confidence should always be in [0, 1]."""
        questions = [
            "Which bars are within 50m of Hollywood Blvd?",
            "How far is Union Station from downtown LA?",
            "What amenity is available at Union Station?",
            "Which restaurant is nearest to Union Station?",
            "What is west of Union Station?",
        ]
        for q in questions:
            result = parser.parse(q)
            assert 0.0 <= result["confidence"] <= 1.0, (
                f"Confidence {result['confidence']} out of range for: {q}"
            )


# ── Concept extraction ───────────────────────────────────────────────

class TestConceptExtraction:
    """Test that concepts are correctly extracted from questions."""

    def test_amenity_extraction(self, parser):
        """OBJECT concept should extract the amenity type."""
        result = parser.parse("Which bars are within 50m of Hollywood Blvd?")
        objects = [c for c in result["concepts"] if c["type"] == "OBJECT"]
        assert len(objects) == 1
        assert objects[0]["text"] == "bar"

    def test_amenity_longest_match(self, parser):
        """Should prefer 'restaurant' over 'rest' (longest match)."""
        result = parser.parse("Which restaurant is nearest to Union Station?")
        objects = [c for c in result["concepts"] if c["type"] == "OBJECT"]
        assert len(objects) == 1
        assert objects[0]["text"] == "restaurant"

    def test_radius_extraction(self, parser):
        """AMOUNT concept should extract the radius string."""
        result = parser.parse("Which bars are within 50m of Hollywood Blvd?")
        amounts = [c for c in result["concepts"] if c["type"] == "AMOUNT"]
        assert len(amounts) == 1
        assert amounts[0]["text"] == "50m"

    def test_location_extraction(self, parser):
        """LOCATION concept should extract the entity name."""
        result = parser.parse("Which restaurant is nearest to Union Station?")
        locations = [c for c in result["concepts"] if c["type"] == "LOCATION"]
        assert len(locations) >= 1
        assert locations[0]["text"] == "Union Station"

    def test_bus_station_amenity_extraction(self, parser):
        """Rare transit amenity vocab must still extract OBJECT.

        Regression: the trained concept model gives OBJECT prob ~0.4963
        (just under the 0.5 threshold) for "bus station" because "station"
        is dominated by LOCATION usage in the MapQA training data. The
        heuristic safety net must recover it, and the closed-vocab matcher
        must return the canonical underscore form ("bus_station") so the
        executor's exact-tag tier hits the real OSM tag value.
        """
        result = parser.parse("bus station within 50km of Le Petit Parisien")
        objects = [c for c in result["concepts"] if c["type"] == "OBJECT"]
        assert len(objects) == 1
        assert objects[0]["text"] == "bus_station"

    @pytest.mark.parametrize("question,expected", [
        ("Which museums are within 2km of Belfast?", "museum"),
        ("Which beaches are within 5km of Belfast?", "beach"),
        ("Which parks are within 2km of Belfast?", "park"),
    ])
    def test_rare_amenity_object_extraction(self, parser, question, expected):
        """Regression (2026-09-19): 'Which museums are within 2km of
        Belfast?' extracted no OBJECT (the trained concept model missed the
        rare vocabulary and the heuristic back-in lacked it), so the
        executor skipped the search entirely and the research summary
        reported "no museums found". The heuristic OBJECT back-in now
        carries the documented sample_questions.md amenity vocabulary."""
        result = parser.parse(question)
        objects = [c for c in result["concepts"] if c["type"] == "OBJECT"]
        assert len(objects) == 1, "no OBJECT concept extracted for %r" % question
        assert expected in objects[0]["text"]


# ── DAG validation ───────────────────────────────────────────────────

class TestDagValidation:
    """Test that the DAG is valid (G2 role precedence invariant)."""

    @pytest.mark.parametrize("question", [
        "Which bars are within 50m of Hollywood Blvd?",
        "How far is Union Station from downtown LA?",
        "What amenity is available at Union Station?",
        "Which restaurant is nearest to Union Station?",
        "What is west of Union Station?",
    ])
    def test_dag_valid(self, parser, question):
        """DAG should be valid for all 5 template questions."""
        result = parser.parse(question)
        assert result["validation"]["valid"], (
            f"DAG invalid for: {question}\n"
            f"Violations: {result['validation'].get('violations', [])}"
        )

    def test_role_precedence(self, parser):
        """G2: SUB_COND (1) < COND (2) < SUPPORT (3) < MEASURE (4)."""
        result = parser.parse("Which bars are within 50m of Hollywood Blvd?")
        roles = result["roles"]
        if len(roles) >= 2:
            # Check that roles are in non-decreasing order
            from semantic_search.services.query_parser_service import ROLE_ORDER
            order_values = [ROLE_ORDER.get(r["role"], 99) for r in roles]
            for i in range(1, len(order_values)):
                assert order_values[i] >= order_values[i - 1], (
                    f"Role precedence violated: {roles}"
                )


# ── Question-word typo tolerance ─────────────────────────────────────

class TestQuestionWordTypoTolerance:
    """'Whichs'/'Wich' typos must not defeat compare-closer patterns or
    leak into entity extraction.

    Regression: "Whichs is closer to Moher Cottage: Cliff Coast Coffee or
    the Cliffs of Moher?" previously extracted 'Whichs' as an entity and
    split 'Cliffs of Moher' into fragments, cascading into a wrong anchor
    ("Moher" → "Moher West") and a truncated-span false positive
    ("Cliffs" → "Cliffs of Howth").
    """

    def test_extract_all_entities_tolerates_question_word_typo(self):
        from semantic_search.services.query_parser_service import QueryParserService
        entities = QueryParserService.extract_all_entities(
            "Whichs is closer to Moher Cottage: Cliff Coast Coffee or the "
            "Cliffs of Moher?"
        )
        # "the" is preserved by the compare pattern; EntityGeocoder strips
        # leading articles before resolution.
        assert entities == [
            "Cliff Coast Coffee", "the Cliffs of Moher", "Moher Cottage",
        ]

    def test_parse_typo_question_clean_concepts(self, parser):
        result = parser.parse(
            "Whichs is closer to Moher Cottage: Cliff Coast Coffee or the "
            "Cliffs of Moher?"
        )
        assert result["template"] == "GEOCODE-BATCH-COMPARE (#4)"
        texts = " ".join(c["text"] or "" for c in result["concepts"])
        assert "Whichs" not in texts
        assert "Moher Cottage" in texts

    def test_correct_spelling_unchanged(self):
        """Correctly-spelled question words must survive normalization."""
        from semantic_search.services.query_parser_service import QueryParserService
        q = "Which is closer to Moher Cottage: Cliff Coast Coffee or the Cliffs of Moher?"
        assert QueryParserService._normalize_question_words(q) == q

    def test_entity_like_word_not_clobbered(self):
        """Real entity words edit-close to question words must survive
        normalization ('Mill' → 'Will' is 1 edit, 'What' → 'Who' is 2 —
        but neither is a prefix-linked typo)."""
        from semantic_search.services.query_parser_service import QueryParserService
        q = "Which is closer to Dublin: Tully Mill, Betelnut or Moher?"
        assert QueryParserService._normalize_question_words(q) == q
        assert QueryParserService._normalize_question_words(
            "What is closer to Heavens cafe: Roshan Cafe or Pipels Cafe ?"
        ) == "What is closer to Heavens cafe: Roshan Cafe or Pipels Cafe ?"


# ── Multi-entity extraction ──────────────────────────────────────────

class TestMultiEntityExtraction:
    """Test extract_all_entities for multi-entity templates."""

    @pytest.fixture
    def extractor(self):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
        import django
        django.setup()
        from semantic_search.services.query_parser_service import QueryParserService
        return QueryParserService.extract_all_entities

    def test_distance_pattern(self, extractor):
        """'How far is X from Y?' → [X, Y]"""
        entities = extractor("How far is Union Station from downtown LA?")
        assert entities == ["Union Station", "downtown LA"]

    def test_compare_closer_pattern(self, extractor):
        """'Which is closer to Z: X or Y?' → [X, Y, Z]"""
        entities = extractor("Which is closer to Union Station: Starbucks or McDonalds?")
        assert entities == ["Starbucks", "McDonalds", "Union Station"]

    def test_compare_closer_what_pattern(self, extractor):
        """'What is closer to Z: X or Y?' → [X, Y, Z] (anchor last).

        Regression: only "which" matched before, so the capitalization
        fallback returned question order ([Z, X, Y]) and the anchor was
        wrong when the parser's bogus OBJECT ("is closer") was ignored.
        """
        entities = extractor(
            "What is closer to Heavens cafe: Roshan Cafe or Pipels Cafe ?",
        )
        assert entities == ["Roshan Cafe", "Pipels Cafe", "Heavens cafe"]

    def test_compare_closer_comma_list(self, extractor):
        """'Which is closer to Z: A, B or C?' → [A, B, C, Z]"""
        entities = extractor("Which is closer to Dublin: Tully Mill, Betelnut or Moher?")
        assert entities == ["Tully Mill", "Betelnut", "Moher", "Dublin"]

    def test_compare_candidates_helper(self):
        from semantic_search.services.query_parser_service import QueryParserService
        assert QueryParserService._compare_candidates(
            "A, B", "C", "Dublin",
        ) == ["A", "B", "C", "Dublin"]
        assert QueryParserService._compare_candidates(
            "Tully Mill", "Moher", "Dublin",
        ) == ["Tully Mill", "Moher", "Dublin"]

    def test_single_entity_fallback(self, extractor):
        """Single capitalized entity → [entity]"""
        entities = extractor("What amenity is available at Union Station?")
        # Should extract at least "Union Station"
        assert "Union Station" in entities

    # ── Cardinal direction exclusion (5b cone-search reachability) ──────
    # Regression: capitalized cardinals ("West", "East", etc.) were being
    # extracted as LOCATION entities by the fallback tokenizer, causing the
    # multi-entity supplement to inflate locations to 2 and bypass the 5b
    # cone-search branch in the executor.  See
    # docs/issues/resolved/MAPQA_LOCATION_BEARING_5B_CONE_SEARCH_GAP.md

    @pytest.mark.parametrize("cardinal", [
        "North", "South", "East", "West",
        "Northeast", "Southeast", "Southwest", "Northwest",
    ])
    def test_cardinal_not_extracted_as_entity(self, extractor, cardinal):
        """Cardinal directions must never appear in extracted entities."""
        entities = extractor(f"What is {cardinal} of Friar Tavern?")
        assert cardinal not in entities, (
            f"Cardinal '{cardinal}' was extracted as an entity: {entities}"
        )
        assert "Friar Tavern" in entities

    def test_5b_question_yields_single_entity(self, extractor):
        """'What is west of X?' should yield only X, not [West, X]."""
        entities = extractor("What is west of Tullygally Tavern?")
        assert "West" not in entities
        assert entities == ["Tullygally Tavern"]

    def test_5b_with_amenity_yields_anchor_only(self, extractor):
        """'What is the nearest cafe east of X?' → only X (amenity is OBJECT, not LOCATION)."""
        entities = extractor("What is the nearest cafe east of Union Station?")
        assert "East" not in entities
        assert "Union Station" in entities


# ── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    """Test edge cases and robustness."""

    def test_empty_question(self, parser):
        """Empty question should not crash."""
        result = parser.parse("")
        assert "template" in result
        assert "concepts" in result

    def test_unknown_question(self, parser):
        """Question with no recognizable pattern should still return a result."""
        result = parser.parse("What is the meaning of life?")
        assert "template" in result
        assert "confidence" in result

    def test_long_question(self, parser):
        """Very long question should not crash."""
        long_q = "Which bars are within 50m of Hollywood Blvd? " * 20
        result = parser.parse(long_q)
        assert "template" in result
