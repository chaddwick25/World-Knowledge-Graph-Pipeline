"""
Tests for the tag-phrase gate (space ↔ underscore round-trip).

Natural phrases ("bubble tea") and stored OSM tag values ("bubble_tea")
differ by whitespace, and searchers type either form. The gate
normalizes both directions: dict lookups resolve space-form keys,
tags__contains filters normalize to the stored underscore convention
(name exempt upstream), and the research matcher matches either form.

Regression anchor: the 2026-10-11 Ireland run — "bubble tea" was
unresolvable everywhere (planner vocabulary, tool description,
TAG_RULES), so the intent survived only as an incidental cuisine tally
("Bubble Tea (×3)") instead of driving the questions. No DB / network
required.
"""

from semantic_search.services.query_executor_service._constants import (
    normalize_tag_phrase,
    normalize_tag_value,
    tag_value_variants,
)
from semantic_search.services.query_executor_service.template_executors import (
    TemplateExecutorsMixin,
)
from semantic_search.services.research_service import ResearchOrchestratorService

resolve = TemplateExecutorsMixin._resolve_tag_target


def test_normalize_tag_phrase_space_and_underscore_forms():
    assert normalize_tag_phrase("bubble tea") == "bubble tea"
    assert normalize_tag_phrase("bubble_tea") == "bubble tea"
    assert normalize_tag_phrase("Bubble Tea") == "bubble tea"
    assert normalize_tag_phrase("  Bubble_Tea  are ") == "bubble tea"


def test_normalize_tag_value_to_stored_convention():
    assert normalize_tag_value("bubble tea") == "bubble_tea"
    assert normalize_tag_value("bubble_tea") == "bubble_tea"
    assert normalize_tag_value("  Bubble Tea ") == "bubble_tea"


def test_tag_value_variants_from_either_form():
    assert tag_value_variants("bubble tea") == ("bubble tea", "bubble_tea")
    assert tag_value_variants("bubble_tea") == ("bubble tea", "bubble_tea")
    assert tag_value_variants("") == ()


def test_resolve_tag_target_bubble_tea_all_forms():
    assert resolve("bubble tea") == ("cuisine", "bubble_tea")
    assert resolve("Bubble Tea") == ("cuisine", "bubble_tea")
    assert resolve("bubble_tea") == ("cuisine", "bubble_tea")
    assert resolve("bubble teas") == ("cuisine", "bubble_tea")


def test_resolve_tag_target_compound_head_noun_drop():
    assert resolve("bubble tea cafes") == ("cuisine", "bubble_tea")
    assert resolve("ice cream shops") == ("amenity", "ice_cream")
    assert resolve("bubble tea cafe are") == ("cuisine", "bubble_tea")


def test_resolve_tag_target_regressions_unchanged():
    assert resolve("hotels") == ("tourism", "hotel")
    assert resolve("peaks") == ("natural", "peak")
    assert resolve("waterfalls") == ("waterway", "waterfall")
    assert resolve("guest houses") == ("tourism", "guest_house")
    assert resolve("cafe") == ("amenity", "cafe")
    assert resolve("nature reserve") == ("leisure", "nature_reserve")
    assert resolve("taco vendors") == ("amenity", "restaurant")
    assert resolve("shoes") is None
    assert resolve("") is None


def test_generic_amenity_keys_regression():
    assert TemplateExecutorsMixin._generic_amenity_keys("amenities are") is not None
    assert TemplateExecutorsMixin._generic_amenity_keys("shoes") is None


def test_research_matcher_matches_bubble_tea_tags():
    match = ResearchOrchestratorService._entity_matches
    cafe = {"tags": {"amenity": "cafe", "cuisine": "bubble_tea"}}
    shop = {"tags": {"shop": "supermarket"}}
    assert match(cafe, "bubble tea") is True
    assert match(shop, "bubble tea") is False


def test_research_matcher_matches_space_stored_variant():
    match = ResearchOrchestratorService._entity_matches
    entity = {"tags": {"cuisine": "bubble tea"}}
    assert match(entity, "bubble tea") is True
