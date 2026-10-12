"""
Tests for the doppelganger reorganization (2026-10-11): the
similarEntities tool, the pre-plan tool round, and the
DOPPELGANGER_REPORT recipe routing.

The Friars Tavern run answered "find similar entities to X" with a
proximity census because no representation for "similar" existed: no
recipe, no tool, no entity-seeded k-NN. These tests cover the pure
logic: recipe routing, the tool manifest schema, pre-plan wiring with a
mocked LLM, and the evidence text. The service's k-NN query was
verified live in the container (Madigan's etc. for the Friars Tavern
seed). No DB / network required.
"""

import pytest

from semantic_search.services.research_recipes import (
    DOPPELGANGER_REPORT,
    FOOD_REPORT,
    route_to_recipe,
)
from semantic_search.services.query_enrichment_service import (
    QueryEnrichmentService,
)
from semantic_search.services.research_service import ResearchOrchestratorService


def test_route_doppelganger_prompts():
    assert route_to_recipe(
        "find similar entities to The Friars Tavern"
    ) is DOPPELGANGER_REPORT
    assert route_to_recipe("twins of The Friars Tavern") is DOPPELGANGER_REPORT
    assert route_to_recipe(
        "What is similar to the Temple Bar district?"
    ) is DOPPELGANGER_REPORT


def test_route_food_prompts_stay_default():
    assert route_to_recipe(
        "Where can you eat and drink in Belize?"
    ) is FOOD_REPORT
    assert route_to_recipe("tell me about taverns in ireland") is FOOD_REPORT


def test_route_seed_line_brief_routes_doppelganger():
    # The brief handshake: the finalize appends "Seed:" (on its own line)
    # only for doppelganger asks it verified; the run's prompt (the
    # brief) carries the marker.
    brief = (
        "Research IE (all) focusing on The Friars Tavern.\n"
        "Seed: The Friars Tavern (pub)"
    )
    assert route_to_recipe(brief) is DOPPELGANGER_REPORT
    assert route_to_recipe(
        "Research IE focusing on cafes. Constraints: none"
    ) is FOOD_REPORT


def test_similarentities_in_tools_manifest():
    schema = QueryEnrichmentService._research_tools_schema()
    names = {s["function"]["name"] for s in schema}
    assert "similarEntities" in names
    spec = next(
        s["function"] for s in schema if s["function"]["name"] == "similarEntities"
    )
    assert spec["parameters"]["required"] == ["entity"]
    assert set(spec["parameters"]["properties"]) == {"entity", "topK", "scope"}


def test_validate_tool_calls_accepts_similarentities():
    calls = [{
        "name": "similarEntities",
        "arguments": {"entity": "The Friars Tavern", "topK": 8},
    }]
    validated = QueryEnrichmentService._validate_tool_calls(calls)
    assert validated == [
        ("similarEntities", {"entity": "The Friars Tavern", "topK": 8}),
    ]


def test_tool_evidence_text():
    calls = [
        {"tool": "similarEntities", "args": {}, "output": [
            {"name": "Madigan's", "country_code": "IE"},
            {"name": "The Doffer", "country_code": "GB"},
        ]},
        {"tool": "structuredSearch", "args": {}, "output": []},
    ]
    text = ResearchOrchestratorService._tool_evidence_text(calls)
    assert "similarEntities: Madigan's (IE); The Doffer (GB)" in text
    assert "structuredSearch" not in text


def test_preplan_tools_executes_and_emits(monkeypatch):
    from semantic_search.services import research_service as rs
    # The settings default is '0' locally (compose sets '1' for the
    # container); the round's gate is the module flag either way.
    monkeypatch.setattr(rs, "_FOLLOWUP_ENABLED", True)
    # chat_tools returns the RAW native calls (dicts); _validate_tool_calls
    # consumes that shape and produces the (tool, args) tuples.
    calls = [{
        "name": "similarEntities",
        "arguments": {"entity": "The Friars Tavern"},
    }]
    outputs = []

    class FakeLLM:
        @staticmethod
        def chat_tools(messages, tools=None, temperature=0.0, max_tokens=300):
            return None, calls

    monkeypatch.setattr(
        QueryEnrichmentService, "_call_search_tool",
        classmethod(lambda cls, tool, args, results, cc, sd: [
            {"name": "Madigan's", "country_code": "IE"}]),
    )
    events = []
    out = ResearchOrchestratorService._preplan_tools(
        FakeLLM(), "find similar entities to The Friars Tavern",
        "The Friars Tavern", "IE", None,
        lambda e: events.append(e),
    )
    assert len(out) == 1
    assert out[0]["tool"] == "similarEntities"
    assert out[0]["output"][0]["name"] == "Madigan's"
    assert [e["event"] for e in events] == ["tool", "tool_out"]


def test_preplan_tools_disabled_returns_empty(monkeypatch):
    from semantic_search.services import research_service as rs
    monkeypatch.setattr(rs, "_FOLLOWUP_ENABLED", False)
    out = ResearchOrchestratorService._preplan_tools(
        None, "prompt", None, "IE", None, None,
    )
    assert out == []


def test_subject_constraint_entity_variant():
    line = ResearchOrchestratorService._subject_constraint_line(
        "The Friars Tavern"
    )
    assert "named entity" in line
    assert "plan around" in line
    # The class variant still carries the class instruction.
    class_line = ResearchOrchestratorService._subject_constraint_line(
        "bubble tea"
    )
    assert "carry the subject as the class" in class_line


# ── Seed verification (the confirmation flow) ────────────────────────────

def test_verify_seed_skips_non_doppelganger_briefs():
    assert ResearchOrchestratorService._verify_seed(
        "Research Ireland focusing on cafes", "IE", None,
    ) is None


def test_verify_seed_resolves_with_geocode(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 9842380892, "osm_type": "node",
            "name": "The Friars Tavern", "lat": 53.09, "lon": -8.12,
            "tags": {"name": "The Friars Tavern", "amenity": "pub"},
        }),
    )
    seed = ResearchOrchestratorService._verify_seed(
        "find similar entities to The Friars Tavern",
        "Research Ireland focusing on The Friars Tavern (taverns)",
        "IE", None,
    )
    assert seed["found"] is True
    assert seed["name"] == "The Friars Tavern"
    assert seed["osm_id"] == 9842380892


def test_verify_seed_reports_not_found(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: None),
    )
    seed = ResearchOrchestratorService._verify_seed(
        "find a doppelganger for a tavern",
        "Research Ireland focusing on Friers Tavern (taverns)",
        "IE", None,
    )
    assert seed == {"found": False, "asked": "Friers Tavern"}


def test_append_seed_line_found_and_missing():
    brief = "Research Ireland focusing on The Friars Tavern (taverns)"
    found = ResearchOrchestratorService._append_seed_line(brief, {
        "found": True, "name": "The Friars Tavern",
        "tags": {"amenity": "pub"},
    })
    assert found.endswith("Seed: The Friars Tavern (pub)")
    missing = ResearchOrchestratorService._append_seed_line(brief, {
        "found": False, "asked": "Friers Tavern",
    })
    assert "Seed: 'Friers Tavern' not found in the dataset" in missing


def test_finalize_brief_carries_seed(monkeypatch):
    from semantic_search.services import research_service as rs
    from semantic_search.services.entity_geocoder import EntityGeocoder

    class FakeLLM:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def chat_json(messages, temperature=0.0, max_tokens=300, **kw):
            return {"area": "Ireland", "subject": "The Friars Tavern",
                    "focus": "taverns", "scope": "", "constraints": ""}

    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance",
        staticmethod(lambda: FakeLLM()),
    )
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 9842380892, "osm_type": "node",
            "name": "The Friars Tavern", "lat": 53.09, "lon": -8.12,
            "tags": {"name": "The Friars Tavern", "amenity": "pub"},
        }),
    )
    out = ResearchOrchestratorService.finalize_brief(
        [{"role": "user", "content": "find a doppelganger for a tavern"}],
        country_code="IE",
    )
    assert out["source"] == "structured"
    assert out["seed"]["found"] is True
    assert "Seed: The Friars Tavern (pub)" in out["brief"]


def test_finalize_routes_on_the_full_interview(monkeypatch):
    # The 2026-10-11 Jamaica run: the user typed "similar entites"
    # (misspelled) and the brief carried no similarity signal, so the run
    # went to FOOD_REPORT and the doppelganger machinery never engaged.
    # The KE's own wording ("looking for doppelgangers of?") carries the
    # intent: routing reads the full interview, not just the first
    # message.
    from semantic_search.services import research_service as rs
    from semantic_search.services.entity_geocoder import EntityGeocoder

    class FakeLLM:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def chat_json(messages, temperature=0.0, max_tokens=300, **kw):
            return {"area": "St James", "subject": "Father Bull Jerk",
                    "focus": "jerk", "scope": "", "constraints": ""}

    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance",
        staticmethod(lambda: FakeLLM()),
    )
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 42, "osm_type": "node", "name": "Father Bull Jerk",
            "lat": 18.38, "lon": -77.92,
            "tags": {"name": "Father Bull Jerk", "amenity": "restaurant"},
        }),
    )
    out = ResearchOrchestratorService.finalize_brief(
        [
            {"role": "user", "content": "find me similar entites to a jerk spot"},
            {"role": "assistant",
             "content": "What is the exact name of the place you're "
                        "looking for doppelgangers of?"},
            {"role": "user", "content": "Father Bull Jerk"},
        ],
        country_code="JM",
    )
    assert out["seed"]["found"] is True
    assert out["seed"]["name"] == "Father Bull Jerk"
    assert "Seed: Father Bull Jerk (restaurant)" in out["brief"]


def test_derived_tallies_local_by_name():
    # The Jamaica run: untagged local places ("Father Bull Jerk" carries
    # no cuisine tag) — the name match surfaces them for the
    # local/international split.
    records = [
        {"cuisine_counts": {"japanese": 1, "seafood": 1},
         "local_name_places": ["Father Bull Jerk", "Pepper's Jerk Center"]},
        {"cuisine_counts": {"italian": 1}, "local_name_places": []},
    ]
    text = ResearchOrchestratorService._derived_tallies(records, "JM")
    assert "local-cuisine places (name match): " \
           "Father Bull Jerk, Pepper's Jerk Center" in text
    assert "other: japanese ×1" in text


def test_local_name_places_construction():
    # The record construction's name match: JM tokens ("jerk",
    # "jamaican", "caribbean") against entity names, underscore forms too.
    from semantic_search.services.research_service import COUNTRY_CUISINE
    tokens = COUNTRY_CUISINE["JM"]
    names = ["Father Bull Jerk", "Redstep Seafood Bar & Grill",
             "Sri Lankan Hut"]
    hits = [
        n for n in names
        if any(t in n.lower() or t.replace("_", " ") in n.lower()
               for t in tokens)
    ]
    assert hits == ["Father Bull Jerk"]
