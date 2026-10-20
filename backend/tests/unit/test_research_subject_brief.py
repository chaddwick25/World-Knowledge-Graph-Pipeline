"""
Tests for the research brief's subject field (intent-as-class-constraint).

The 2026-10-11 Ireland runs lost the interview's subject ("bubble tea")
at brief extraction: the schema had no field for the thing being
researched, so the planner asked generic vendor questions and the
follow-up structuredSearch calls aimed at cafe/coffee_shop, never
cuisine=bubble_tea. The fix: a subject field in FINALIZE_SYSTEM_PROMPT,
a subject-first brief render, and a deterministic subject line injected
into the decompose prompt. No DB / network required.
"""

from semantic_search.services.research_service import ResearchOrchestratorService


def test_render_brief_subject_first_with_focus():
    brief = ResearchOrchestratorService._render_brief({
        "area": "Ireland And Northern Ireland",
        "subject": "bubble tea",
        "focus": "cafe, shops and restaurants",
    })
    assert brief == (
        "Research Ireland And Northern Ireland "
        "focusing on bubble tea (cafe, shops and restaurants)"
    )


def test_render_brief_subject_only():
    brief = ResearchOrchestratorService._render_brief({
        "area": "Ireland", "subject": "bubble tea",
    })
    assert brief == "Research Ireland focusing on bubble tea"


def test_render_brief_focus_only_legacy_shape():
    brief = ResearchOrchestratorService._render_brief({
        "area": "Ireland", "focus": "cafes",
    })
    assert brief == "Research Ireland focusing on cafes"


def test_subject_from_brief_parses_subject_before_paren():
    assert ResearchOrchestratorService._subject_from_brief(
        "Research Ireland And Northern Ireland focusing on bubble tea "
        "(cafe, shops and restaurants). Constraints: name and location"
    ) == "bubble tea"


def test_subject_from_brief_legacy_focus_brief():
    assert ResearchOrchestratorService._subject_from_brief(
        "Research Belize City (within 3km) focusing on restaurants and shops."
    ) == "restaurants and shops"


def test_subject_from_brief_none_without_focus():
    assert ResearchOrchestratorService._subject_from_brief(
        "Research Ireland And Northern Ireland"
    ) is None


def test_subject_constraint_line():
    line = ResearchOrchestratorService._subject_constraint_line("bubble tea")
    assert "Report subject: bubble tea" in line
    assert "Which bubble tea cafes are within X of Y?" in line
    assert ResearchOrchestratorService._subject_constraint_line(None) == ""


def test_inject_subject_question_when_resolvable():
    recipe = {"slots": [{"id": "eat"}], "subject_head": "cafes"}
    questions = [{
        "question": "Which restaurants are within 3000m of Dublin?",
        "slot": "eat", "radius_m": 3000,
    }]
    out = ResearchOrchestratorService._inject_subject_question(
        questions, "bubble tea", recipe,
    )
    assert len(out) == 2
    injected = out[-1]
    assert injected["question"] == (
        "Which bubble tea cafes are within 3 km of Dublin?"
    )
    assert injected["slot"] == "eat"
    assert injected["radius_m"] == 3000
    assert "cuisine=bubble_tea" in injected["why"]


def test_inject_skips_unresolvable_subject():
    recipe = {"slots": [{"id": "eat"}], "subject_head": "cafes"}
    questions = [{
        "question": "Which restaurants are within 3000m of Dublin?",
        "slot": "eat", "radius_m": 3000,
    }]
    out = ResearchOrchestratorService._inject_subject_question(
        questions, "public transit", recipe,
    )
    assert len(out) == 1


def test_inject_skips_when_subject_already_asked():
    recipe = {"slots": [{"id": "eat"}], "subject_head": "cafes"}
    questions = [{
        "question": "Which bubble tea cafes are within 3km of Dublin?",
        "slot": "eat", "radius_m": 3000,
    }]
    out = ResearchOrchestratorService._inject_subject_question(
        questions, "bubble tea", recipe,
    )
    assert len(out) == 1


def test_inject_noop_on_empty_plan():
    assert ResearchOrchestratorService._inject_subject_question(
        [], "bubble tea", {"slots": [{"id": "eat"}]},
    ) == []
