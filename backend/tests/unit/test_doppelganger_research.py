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
    ***REMOVED*** The brief handshake: the finalize appends "Seed:" (on its own line)
    ***REMOVED*** only for doppelganger asks it verified; the run's prompt (the
    ***REMOVED*** brief) carries the marker.
    brief = (
        "Research IE (all) focusing on The Friars Tavern.\n"
        "Seed: The Friars Tavern (pub)"
    )
    assert route_to_recipe(brief) is DOPPELGANGER_REPORT
    assert route_to_recipe(
        "Research IE focusing on cafes. Constraints: none"
    ) is FOOD_REPORT


def test_route_similar_noun_phrasings():
    ***REMOVED*** The 2026-10-12 Redbones runs: "similar cafe(s) to X" (a noun
    ***REMOVED*** between similar and to) missed the alternation and routed to the
    ***REMOVED*** food census, which answers "similar" with "nearby".
    assert route_to_recipe(
        "find me similar cafes to Redbones Blues Cafe in Kingston"
    ) is DOPPELGANGER_REPORT
    assert route_to_recipe(
        "find me a similar cafe to Redbones Blues Cafe in Kingston"
    ) is DOPPELGANGER_REPORT


def test_route_misspelled_doppelganger():
    ***REMOVED*** The third Redbones run: "Dopplegangers" (transposed) defeated the
    ***REMOVED*** strict \bdoppelg\w*\b spelling; every real word starting "dopp"
    ***REMOVED*** is this ask.
    assert route_to_recipe(
        "Find me Dopplegangers of Redbones Blues Cafe located in "
        "Kingston but search Island/country wide"
    ) is DOPPELGANGER_REPORT


def test_subject_from_misspelled_ask_with_location():
    ***REMOVED*** The misspelled form + the "located in" trailing phrase: the
    ***REMOVED*** subject is the similarity target, not the location.
    assert ResearchOrchestratorService._subject_from_brief(
        "Find me Dopplegangers of Redbones Blues Cafe located in "
        "Kingston but search Island/country wide"
    ) == "Redbones Blues Cafe"


def test_finalize_fallback_routes_on_the_conversation(monkeypatch):
    ***REMOVED*** The third Redbones run: the extraction failed, the fallback
    ***REMOVED*** routed on the misspelled user text alone, and the run lost the
    ***REMOVED*** route even though the KE's reply spelled "doppelgangers"
    ***REMOVED*** correctly. The fallback now routes on the FULL conversation (the
    ***REMOVED*** Jamaica-run lesson applied to the fallback path).
    from semantic_search.services.entity_geocoder import EntityGeocoder

    class DownLLM:
        @staticmethod
        def is_available():
            return False

    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance",
        staticmethod(lambda: DownLLM()),
    )
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 5171395621, "osm_type": "node",
            "name": "Redbones Blues Cafe", "lat": 18.01, "lon": -76.78,
            "tags": {"name": "Redbones Blues Cafe", "amenity": "cafe"},
        }),
    )
    out = ResearchOrchestratorService.finalize_brief(
        [
            {"role": "user",
             "content": "Find me Dopplegangers of Redbones Blues Cafe "
                        "located in Kingston but search Island/country "
                        "wide"},
            {"role": "assistant",
             "content": "What is the exact name of the place you're "
                        "looking for doppelgangers of?"},
            {"role": "user", "content": "Redbones Blues Cafe"},
        ],
        country_code="JM",
    )
    assert out["source"] == "fallback"
    assert out["seed"]["found"] is True
    assert "Seed: Redbones Blues Cafe (cafe)" in out["brief"]
    assert route_to_recipe(out["brief"]) is DOPPELGANGER_REPORT


def test_subject_from_raw_similar_ask():
    ***REMOVED*** The fallback brief is the user's own words: the similarity TARGET
    ***REMOVED*** is the subject; the trailing location phrase is not.
    assert ResearchOrchestratorService._subject_from_brief(
        "find me similar cafes to Redbones Blues Cafe in Kingston"
    ) == "Redbones Blues Cafe"
    assert ResearchOrchestratorService._subject_from_brief(
        "similar to Redbones Blues cafe in terms of infrastructure"
    ) == "Redbones Blues cafe"
    assert ResearchOrchestratorService._subject_from_brief(
        "twins of The Friars Tavern"
    ) == "The Friars Tavern"
    ***REMOVED*** Non-similarity asks still yield None (no invented subject).
    assert ResearchOrchestratorService._subject_from_brief(
        "tell me about taverns in ireland"
    ) is None


def test_finalize_fallback_verifies_seed(monkeypatch):
    ***REMOVED*** The extraction failed (LLM down): the fallback brief is the user's
    ***REMOVED*** own words, and it goes through the same verify-and-mark step (the
    ***REMOVED*** 2026-10-12 Redbones run: the fallback skipped verification and the
    ***REMOVED*** run lost the route).
    from semantic_search.services.entity_geocoder import EntityGeocoder

    class DownLLM:
        @staticmethod
        def is_available():
            return False

    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance",
        staticmethod(lambda: DownLLM()),
    )
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 5171395621, "osm_type": "node",
            "name": "Redbones Blues Cafe", "lat": 18.01, "lon": -76.78,
            "tags": {"name": "Redbones Blues Cafe", "amenity": "cafe"},
        }),
    )
    out = ResearchOrchestratorService.finalize_brief(
        [{"role": "user",
          "content": "find me similar cafes to Redbones Blues Cafe "
                     "in Kingston"}],
        country_code="JM",
    )
    assert out["source"] == "fallback"
    assert out["seed"]["found"] is True
    assert "Seed: Redbones Blues Cafe (cafe)" in out["brief"]
    ***REMOVED*** The marked fallback routes the run to the doppelganger report.
    assert route_to_recipe(out["brief"]) is DOPPELGANGER_REPORT


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


def test_tool_evidence_text_class_drift_dict():
    ***REMOVED*** The classDrift executor returns a dict, not a row list: the
    ***REMOVED*** evidence loop must render one compact line, not crash
    ***REMOVED*** (TypeError: unhashable type: 'slice', found 2026-10-17).
    calls = [
        {"tool": "classDrift", "args": {}, "output": {
            "fingerprint": {
                "snapshot_date": "2025_12_31",
                "top_classes": {"building": 22.1, "amenity=cafe": 8.0},
                "shannon_entropy": 3.2,
                "unique_classes": 214,
                "total_entities": 368942,
            },
            "drift": {
                "pairs": 1,
                "latest": {
                    "from": "2024_06_30", "to": "2025_12_31",
                    "js_divergence": 0.421, "drift_magnitude": "high",
                    "dominant_shift": {"building": 5.2},
                },
            },
        }},
        {"tool": "similarEntities", "args": {}, "output": [
            {"name": "Madigan's", "country_code": "IE"},
        ]},
    ]
    text = ResearchOrchestratorService._tool_evidence_text(calls)
    assert "classDrift: 368942 entities" in text
    assert "top: building 22.1%" in text
    assert "drift high (JS 0.421)" in text
    assert "similarEntities: Madigan's (IE)" in text


def test_tool_evidence_text_class_drift_no_baseline():
    calls = [{"tool": "classDrift", "args": {}, "output": {
        "fingerprint": {"total_entities": 10, "top_classes": {}},
        "drift": None,
    }}]
    text = ResearchOrchestratorService._tool_evidence_text(calls)
    assert "single snapshot, no drift baseline" in text


def test_preplan_tools_executes_and_emits(monkeypatch):
    from semantic_search.services import research_service as rs
    ***REMOVED*** The settings default is '0' locally (compose sets '1' for the
    ***REMOVED*** container); the round's gate is the module flag either way.
    monkeypatch.setattr(rs, "_FOLLOWUP_ENABLED", True)
    ***REMOVED*** chat_tools returns the RAW native calls (dicts); _validate_tool_calls
    ***REMOVED*** consumes that shape and produces the (tool, args) tuples.
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
    ***REMOVED*** The class variant still carries the class instruction.
    class_line = ResearchOrchestratorService._subject_constraint_line(
        "bubble tea"
    )
    assert "carry the subject as the class" in class_line


***REMOVED*** ── Seed verification (the confirmation flow) ────────────────────────────

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
    ***REMOVED*** The 2026-10-11 Jamaica run: the user typed "similar entites"
    ***REMOVED*** (misspelled) and the brief carried no similarity signal, so the run
    ***REMOVED*** went to FOOD_REPORT and the doppelganger machinery never engaged.
    ***REMOVED*** The KE's own wording ("looking for doppelgangers of?") carries the
    ***REMOVED*** intent: routing reads the full interview, not just the first
    ***REMOVED*** message.
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
    ***REMOVED*** The Jamaica run: untagged local places ("Father Bull Jerk" carries
    ***REMOVED*** no cuisine tag) — the name match surfaces them for the
    ***REMOVED*** local/international split.
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
    ***REMOVED*** The record construction's name match: JM tokens ("jerk",
    ***REMOVED*** "jamaican", "caribbean") against entity names, underscore forms too.
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


def test_resolve_recipe_override_wins():
    ***REMOVED*** The doppelganger surface hard-selects its recipe: the override
    ***REMOVED*** wins even when the ask carries no doppelganger vocabulary.
    recipe = ResearchOrchestratorService._resolve_recipe(
        "best bubble tea spots", "doppelganger_report",
    )
    assert recipe.get("key") == "doppelganger_report"


def test_resolve_recipe_unknown_key_routes():
    ***REMOVED*** An unknown key must not silently become the default recipe with
    ***REMOVED*** the surface's expectation unmet: it falls through to the router,
    ***REMOVED*** which reads the ask.
    recipe = ResearchOrchestratorService._resolve_recipe(
        "find doppelgangers of The Friars Tavern", "bogus_recipe",
    )
    assert recipe.get("key") == "doppelganger_report"


def test_resolve_recipe_no_override_routes():
    recipe = ResearchOrchestratorService._resolve_recipe("best cafes", None)
    assert recipe.get("key") == "food_report"


def test_verify_seed_honors_recipe_override(monkeypatch):
    ***REMOVED*** An ask without doppelganger vocabulary still verifies on the
    ***REMOVED*** doppelganger surface: the override bypasses the route gate.
    from semantic_search.services.entity_geocoder import EntityGeocoder
    hit = {"osm_id": 1, "osm_type": "node", "name": "X", "lat": 1.0,
           "lon": 2.0, "tags": {}}
    monkeypatch.setattr(
        EntityGeocoder, "geocode", staticmethod(lambda *a, **k: hit),
    )
    seed = ResearchOrchestratorService._verify_seed(
        "tell me about X", "Research X focusing on X", "JM", None,
        recipe_key="doppelganger_report",
    )
    assert seed and seed["found"] is True and seed["name"] == "X"


def test_verify_seed_no_override_non_doppelganger_ask(monkeypatch):
    ***REMOVED*** /tools surface (no override): a food ask verifies nothing.
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("geocode must not run"))),
    )
    seed = ResearchOrchestratorService._verify_seed(
        "best cafes in town", "Research cafes", "JM", None,
    )
    assert seed is None


def test_finalize_brief_confirmed_seed_skips_geocode(monkeypatch):
    ***REMOVED*** The user's live map pick is authoritative: no re-geocode, the
    ***REMOVED*** Seed line renders from the confirmed row.
    def boom(*a, **k):
        raise AssertionError("_verify_seed must not run for a confirmed seed")
    monkeypatch.setattr(
        ResearchOrchestratorService, "_verify_seed", staticmethod(boom),
    )
    confirmed = {"found": True, "asked": "The Friars Tavern",
                 "name": "The Friars Tavern", "osm_id": 9842380892,
                 "osm_type": "node", "tags": {"amenity": "pub"},
                 "lat": 51.5, "lon": -0.1}
    out = ResearchOrchestratorService.finalize_brief(
        [{"role": "user", "content": "doppelgangers of The Friars Tavern"}],
        "IE", None, recipe_key="doppelganger_report",
        confirmed_seed=confirmed,
    )
    assert out["seed"]["osm_id"] == 9842380892
    assert "Seed: The Friars Tavern (pub)" in out["brief"]


def test_render_ke_digest_fingerprint_and_drift():
    digest = {
        "fingerprint": {
            "snapshot_date": "2025_12_31",
            "top_classes": {"building": 22.1, "amenity=cafe": 8.0},
            "shannon_entropy": 3.2,
            "unique_classes": 214,
            "total_entities": 368942,
        },
        "drift": {
            "pairs": 1,
            "latest": {
                "from": "2024_06_30", "to": "2025_12_31",
                "js_divergence": 0.421, "drift_magnitude": "high",
                "dominant_shift": {"building": 5.2},
            },
        },
    }
    text = ResearchOrchestratorService._render_ke_digest(digest)
    assert "368942 entities" in text
    assert "Drift since 2024_06_30: high (JS 0.421)" in text
    assert "building +5.2%" in text


def test_render_ke_digest_single_snapshot():
    text = ResearchOrchestratorService._render_ke_digest({
        "fingerprint": {"snapshot_date": "2025_12_31", "total_entities": 10,
                        "unique_classes": 3, "top_classes": {}},
        "drift": None,
    })
    assert "Single snapshot: no drift baseline yet" in text


def test_render_ke_digest_empty_is_fail_soft():
    assert ResearchOrchestratorService._render_ke_digest(None) == ""
    assert "No class fingerprint yet" in \
        ResearchOrchestratorService._render_ke_digest({})


def test_chat_messages_injects_digest_and_strips_marker():
    msgs = [
        {"role": "user", "content": "find doppelgangers of a tavern"},
        {"role": "assistant", "content": "[seed] Which place exactly?"},
    ]
    full = ResearchOrchestratorService.chat_messages(
        msgs, "JM", "2025_12_31",
        digest_text="Dataset context:\n- Snapshot 2025_12_31: 10 entities",
    )
    assert "Dataset context:" in full[0]["content"]
    ***REMOVED*** the [seed] marker is protocol, not prose: stripped from LLM text
    assert "[seed]" not in full[2]["content"]
    assert "Which place exactly?" in full[2]["content"]


def test_message_text_keeps_marker_for_gate():
    ***REMOVED*** The live-resolution gate reads the RAW transcript to detect the
    ***REMOVED*** marker; only LLM-facing text strips it.
    raw = ResearchOrchestratorService._message_text(
        {"role": "assistant", "content": "[seed] Which place exactly?"})
    assert "[seed]" in raw


def test_live_seed_part_turn1_doppelganger_ask(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    captured = {}

    def fake_candidates(name, cc=None, sd=None, limit=5, min_sim=0.4):
        captured["name"] = name
        captured["cc"] = cc
        return {"asked": name, "candidates": [
            {"osm_id": 9842380892, "osm_type": "node",
             "name": "The Friars Tavern", "lat": 53.09, "lon": -8.12,
             "tags": {"amenity": "pub"}},
        ]}

    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates", staticmethod(fake_candidates),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [{"role": "user",
          "content": "find doppelgangers of The Friars Tavern"}],
        recipe_key="doppelganger_report", country_code="IE",
    )
    assert captured["name"] == "The Friars Tavern"
    assert part["type"] == "data-seed" and part["id"] == "seed"
    assert part["data"]["found"] is True
    assert part["data"]["osm_id"] == 9842380892
    assert "candidates" not in part["data"]  ***REMOVED*** single hit: no list


def test_live_seed_part_marker_gated_turn(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda name, cc=None, sd=None, limit=5, min_sim=0.4: {
            "asked": name, "candidates": [
                {"osm_id": 1, "osm_type": "node", "name": name,
                 "lat": 1.0, "lon": 2.0, "tags": {}},
            ]}),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [
            {"role": "user", "content": "find doppelgangers of a tavern"},
            {"role": "assistant", "content": "[seed] Which place exactly?"},
            {"role": "user", "content": "Father Bull Jerk"},
        ],
        recipe_key="doppelganger_report",
    )
    assert part["data"]["found"] is True
    assert part["data"]["asked"] == "Father Bull Jerk"


def test_live_seed_part_absent_on_non_answer_turns(monkeypatch):
    ***REMOVED*** No [seed] marker on the last assistant turn, no turn-1 ask: no
    ***REMOVED*** resolution, no part (axis/constraint turns never geocode).
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not geocode"))),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [
            {"role": "user", "content": "find doppelgangers of a tavern"},
            {"role": "assistant", "content": "Got it. Similar how?"},
            {"role": "user", "content": "same cuisine"},
        ],
        recipe_key="doppelganger_report",
    )
    assert part is None


def test_live_seed_part_not_found_carries_suggestions(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda name, cc=None, sd=None, limit=5, min_sim=0.4: {
            "asked": name, "candidates": []}))
    monkeypatch.setattr(
        EntityGeocoder, "seed_suggestions",
        staticmethod(lambda name, cc=None, sd=None, limit=3: [
            {"osm_id": 5, "osm_type": "node", "name": "The Friars Tavern",
             "lat": 1.0, "lon": 2.0, "tags": {}}]))
    part = ResearchOrchestratorService.live_seed_part(
        [{"role": "user", "content": "doppelgangers of Friers Tavern"}],
        recipe_key="doppelganger_report",
    )
    assert part["data"]["found"] is False
    assert part["data"]["suggestions"][0]["name"] == "The Friars Tavern"


def test_live_seed_part_multi_hit_carries_candidates(monkeypatch):
    from semantic_search.services.entity_geocoder import EntityGeocoder
    rows = [
        {"osm_id": 1, "osm_type": "node", "name": "Jerk Center",
         "lat": 18.1, "lon": -77.2, "tags": {}},
        {"osm_id": 2, "osm_type": "node", "name": "Jerk Center 2",
         "lat": 18.2, "lon": -77.3, "tags": {}},
    ]
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda name, cc=None, sd=None, limit=5, min_sim=0.4: {
            "asked": name, "candidates": rows}),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [{"role": "user", "content": "doppelgangers of Jerk Center"}],
        recipe_key="doppelganger_report",
    )
    assert part["data"]["candidates"] == rows
    assert part["data"]["osm_id"] == 1


def test_live_seed_part_no_resolution_on_tools_surface(monkeypatch):
    ***REMOVED*** /tools (no override, food ask): turn 1 does not geocode.
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not geocode"))),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [{"role": "user", "content": "best cafes in Dublin"}],
    )
    assert part is None


def test_live_seed_part_turn1_unextractable_subject_skips(monkeypatch):
    ***REMOVED*** Turn 1 routes as doppelganger but the subject pattern cannot
    ***REMOVED*** extract a name: skip (the KE's [seed] question catches the answer
    ***REMOVED*** next turn) instead of geocoding prose.
    from semantic_search.services.entity_geocoder import EntityGeocoder
    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not geocode"))),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [{"role": "user", "content": "help me find twins"}],
        recipe_key="doppelganger_report",
    )
    assert part is None


def test_live_seed_part_strips_synthetic_seed_handshake(monkeypatch):
    ***REMOVED*** An explicit map pick is announced as "Seed: <name> (kind)" - the
    ***REMOVED*** handshake format. If the KE's [seed] question is still the last
    ***REMOVED*** assistant turn, the gate must geocode the NAME, not the wrapper.
    from semantic_search.services.entity_geocoder import EntityGeocoder
    captured = {}

    def fake_candidates(name, cc=None, sd=None, limit=5, min_sim=0.4):
        captured["name"] = name
        return {"asked": name, "candidates": [
            {"osm_id": 9, "osm_type": "node", "name": "The Friars Tavern",
             "lat": 53.09, "lon": -8.12, "tags": {"amenity": "pub"}},
        ]}

    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates", staticmethod(fake_candidates),
    )
    part = ResearchOrchestratorService.live_seed_part(
        [
            {"role": "user", "content": "find doppelgangers of a tavern"},
            {"role": "assistant", "content": "[seed] Which place exactly?"},
            {"role": "user", "content": "Seed: The Friars Tavern (pub)"},
        ],
        recipe_key="doppelganger_report",
    )
    assert captured["name"] == "The Friars Tavern"
    assert part["data"]["found"] is True
