"""
Tests for the continuation-round guards (radius-only re-ask, anchor
qualifier strip).

The 2026-10-11 Ireland run produced the garbled round-2 question
"Which cuisines are within 6 km of Belfast in Dublin?": the widened-radius
generator was applied to a #4 compare question that has no radius to
widen, and the anchor extraction swallowed the original question's
trailing "in Dublin" qualifier after a placeholder substitution left
"Belfast" glued to it. Two guards: the re-ask fires only for radius
questions, and the anchor strips a single capitalized trailing "in <X>"
qualifier. No DB / network required.
"""

from semantic_search.services.research_service import ResearchOrchestratorService


def test_anchor_strips_trailing_capitalized_qualifier():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which cuisines are near Belfast in Dublin?"
    ) == "Belfast"


def test_anchor_regression_plain_radius_question():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which hotels are within 2km of Belize City?"
    ) == "Belize City"


def test_anchor_keeps_lowercase_multiword_tail():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which cafes are near the cafe in the wood?"
    ) == "the cafe in the wood"


def _continuation_for(qtext, status="class_mismatch"):
    records = [{"question": qtext}]
    unmet = [{"slot": "cuisine", "status": status, "question": qtext}]
    return ResearchOrchestratorService._continuation_questions(
        records, unmet, "IE", None,
    )


def test_continuation_skips_non_radius_questions():
    assert _continuation_for(
        "Which cuisines are near the top restaurant in Dublin?"
    ) == []


def test_continuation_widens_radius_questions():
    follow_ups = _continuation_for(
        "Which restaurants are within 2km of Jerk Town?"
    )
    assert len(follow_ups) == 1
    assert follow_ups[0]["question"] == (
        "Which restaurants are within 6 km of Jerk Town?"
    )
