"""
Tests for the researcher amplification (2026-10-12): the three decision
coefficients computed from resolved rows (researcher amplification).

The pure logic: twin score combination (channels, agreement confidence,
ranking), the Beta-Binomial presence posterior, the run-confidence
composition, the evidence-text rendering, and the repair-ladder presence
gate (env-gated; mocked executor, no DB / network required).
"""

import pytest

from semantic_search.services.evidence_scoring_service import (
    EvidenceScoringService,
)
from semantic_search.services.research_recipes import FOOD_REPORT
from semantic_search.services.research_service import (
    ResearchOrchestratorService,
    _PRESENCE_GATE_ENABLED,
)


***REMOVED*** ── Twin score: channel combination ──────────────────────────────────

def test_twin_score_multi_channel():
    rows = [{
        "osm_id": 1, "name": "Madigan's", "wkg_class": "wkgs:Pub",
        "similarity": 0.9,
    }]
    uslp = {1: 0.8}
    out = EvidenceScoringService.score_candidates(
        "wkgs:Pub", rows, uslp_by_osm_id=uslp,
    )
    ***REMOVED*** weights: cosine .6, class .25, uslp .15 → all channels agree high
    r = out[0]
    assert r["twin_score"] == pytest.approx(
        0.6 * 0.9 + 0.25 * 1.0 + 0.15 * 0.8, abs=1e-4,
    )
    ***REMOVED*** agreement: 1 - (max - min) over {0.9, 1.0, 0.8}
    assert r["twin_confidence"] == pytest.approx(1.0 - 0.2)
    assert r["twin_channels"] == ["class", "cosine", "uslp"]


def test_twin_score_single_channel_confidence_is_none():
    rows = [{"osm_id": 1, "name": "X", "wkg_class": "wkgs:Pub",
             "similarity": 0.7}]
    out = EvidenceScoringService.score_candidates(None, rows)
    assert out[0]["twin_score"] == pytest.approx(0.7)
    ***REMOVED*** A single channel is honest unknown, never faked.
    assert out[0]["twin_confidence"] is None
    assert out[0]["twin_channels"] == ["cosine"]


def test_twin_score_class_mismatch_pulls_down():
    rows = [{"osm_id": 1, "name": "X", "wkg_class": "wkgs:Shop",
             "similarity": 0.9}]
    out = EvidenceScoringService.score_candidates("wkgs:Pub", rows)
    ***REMOVED*** cosine .6 * .9 + class .25 * 0 = .54 / .85 (service rounds to 4dp)
    assert out[0]["twin_score"] == pytest.approx(0.54 / 0.85, abs=1e-4)


def test_twin_score_reranks_by_score():
    rows = [
        {"osm_id": 1, "name": "weak", "wkg_class": "wkgs:Shop",
         "similarity": 0.5},
        {"osm_id": 2, "name": "strong", "wkg_class": "wkgs:Pub",
         "similarity": 0.9},
    ]
    out = EvidenceScoringService.score_candidates("wkgs:Pub", rows)
    assert out[0]["name"] == "strong"
    assert out[1]["name"] == "weak"


def test_twin_score_no_channels_scores_none():
    rows = [{"osm_id": 1, "name": "X"}]
    out = EvidenceScoringService.score_candidates(None, rows)
    assert out[0]["twin_score"] is None
    assert out[0]["twin_confidence"] is None


def test_twin_score_cosine_clamped():
    rows = [{"osm_id": 1, "name": "X", "similarity": 1.2}]
    out = EvidenceScoringService.score_candidates(None, rows)
    assert out[0]["twin_score"] == 1.0


***REMOVED*** ── Presence posterior ───────────────────────────────────────────────

def test_presence_absent_when_dense_probe_has_no_class_matches():
    ***REMOVED*** Dense probe (30 evidence-worthy rows), zero match the asked class:
    ***REMOVED*** the posterior upper bound falls under the bar → absent.
    out = EvidenceScoringService.presence_posterior(0, 30)
    assert out["decision"] == "absent"
    assert out["n_class"] == 0 and out["n_total"] == 30


def test_presence_uncertain_when_sparse_probe():
    ***REMOVED*** Under MIN_PROBE rows the probe says nothing: replan still runs.
    out = EvidenceScoringService.presence_posterior(0, 3)
    assert out["decision"] == "uncertain"


def test_presence_uncertain_when_class_matches_present():
    out = EvidenceScoringService.presence_posterior(2, 30)
    assert out["decision"] == "uncertain"
    ***REMOVED*** alpha = 1 + 2, beta = 4 + 28 → p = 3/35
    assert out["p_present"] == pytest.approx(3 / 35, abs=1e-4)


def test_presence_n_class_clamped_to_total():
    out = EvidenceScoringService.presence_posterior(50, 10)
    assert out["n_class"] == 10


def test_presence_prior_mean_is_weak():
    ***REMOVED*** Zero evidence: the posterior is the prior (mean 0.2), never a
    ***REMOVED*** confident absence.
    out = EvidenceScoringService.presence_posterior(0, 0)
    assert out["decision"] == "uncertain"
    assert out["p_present"] == pytest.approx(0.2)


***REMOVED*** ── Run confidence ───────────────────────────────────────────────────

def test_compose_confidence_bands():
    cov = [
        {"slot": "eat", "status": "filled"},
        {"slot": "spread", "status": "filled_by_repair"},
        {"slot": "move", "status": "missing"},
        {"slot": "x", "status": "error"},
    ]
    out = EvidenceScoringService.compose_confidence(cov)
    assert out["value"] == pytest.approx(0.5)
    assert out["band"] == "medium"
    assert out["components"]["filled"] == 2
    assert out["components"]["total"] == 4
    assert out["components"]["twin_agreement"] is None


def test_compose_confidence_twin_agreement_ignores_none():
    cov = [{"slot": "twins", "status": "filled"}]
    out = EvidenceScoringService.compose_confidence(cov, [0.8, None, 0.6])
    assert out["components"]["twin_agreement"] == pytest.approx(0.7)
    assert out["band"] == "high"


def test_compose_confidence_empty_ledger_is_low():
    out = EvidenceScoringService.compose_confidence([])
    assert out["value"] == 0.0 and out["band"] == "low"


***REMOVED*** ── Wiring: pre-plan twin scoring (fail-soft) ────────────────────────

def test_score_preplan_twins_scores_and_reranks(monkeypatch):
    from semantic_search.services import evidence_scoring_service as es
    from semantic_search.services.entity_geocoder import EntityGeocoder

    calls = [{
        "tool": "similarEntities",
        "args": {"entity": "The Friars Tavern"},
        "output": [
            {"osm_id": 2, "name": "Weak", "wkg_class": "wkgs:Shop",
             "similarity": 0.5},
            {"osm_id": 3, "name": "Strong", "wkg_class": "wkgs:Pub",
             "similarity": 0.9},
        ],
    }]
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: {
            "osm_id": 9842380892, "osm_type": "node",
            "name": "The Friars Tavern", "wkg_class": "wkgs:Pub",
        }),
    )
    monkeypatch.setattr(
        es.EvidenceScoringService, "_uslp_channel",
        staticmethod(lambda seed, rows, cc, sd: {3: 0.8}),
    )
    out = es.EvidenceScoringService.score_preplan_twins(
        calls, "IE", None,
    )
    assert out[0]["name"] == "Strong"
    assert out[0]["twin_confidence"] is not None
    ***REMOVED*** The rows were scored in place: the tool record carries them.
    assert calls[0]["output"][0]["name"] == "Strong"


def test_score_preplan_twins_returns_none_without_similarentities():
    calls = [{"tool": "nameSearch", "args": {}, "output": []}]
    assert EvidenceScoringService.score_preplan_twins(
        calls, "IE", None,
    ) is None


def test_score_preplan_twins_cosine_only_when_seed_missing(monkeypatch):
    from semantic_search.services import evidence_scoring_service as es
    from semantic_search.services.entity_geocoder import EntityGeocoder

    calls = [{
        "tool": "similarEntities",
        "args": {"entity": "Nowhere"},
        "output": [{"osm_id": 1, "name": "X", "similarity": 0.7}],
    }]
    monkeypatch.setattr(
        EntityGeocoder, "geocode",
        staticmethod(lambda name, cc=None, sd=None: None),
    )
    out = es.EvidenceScoringService.score_preplan_twins(calls, "IE", None)
    assert out[0]["twin_score"] == pytest.approx(0.7)
    assert out[0]["twin_confidence"] is None


def test_score_preplan_twins_never_raises(monkeypatch):
    from semantic_search.services import evidence_scoring_service as es
    from semantic_search.services.entity_geocoder import EntityGeocoder

    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(
        EntityGeocoder, "geocode", staticmethod(boom),
    )
    calls = [{
        "tool": "similarEntities", "args": {"entity": "X"},
        "output": [{"osm_id": 1, "name": "X", "similarity": 0.7}],
    }]
    assert es.EvidenceScoringService.score_preplan_twins(
        calls, "IE", None,
    ) is None


***REMOVED*** ── Evidence text rendering ──────────────────────────────────────────

def test_tool_evidence_text_carries_scores():
    calls = [{
        "tool": "similarEntities",
        "args": {},
        "output": [
            {"name": "Madigan's", "country_code": "IE",
             "twin_score": 0.82, "twin_confidence": 0.74},
            {"name": "The Doffer", "country_code": "GB",
             "twin_score": 0.51, "twin_confidence": None},
        ],
    }]
    text = ResearchOrchestratorService._tool_evidence_text(calls)
    assert "Madigan's (IE) [score 0.82, conf 0.74]" in text
    assert "The Doffer (GB) [score 0.51]" in text


def test_tool_evidence_text_unscored_rows_unchanged():
    calls = [{
        "tool": "similarEntities", "args": {},
        "output": [{"name": "Madigan's", "country_code": "IE"}],
    }]
    text = ResearchOrchestratorService._tool_evidence_text(calls)
    assert text == "- similarEntities: Madigan's (IE)"


***REMOVED*** ── Repair ladder: the presence gate ─────────────────────────────────

def _run_repair(monkeypatch, probe_rows, presence_gate):
    """One empty ***REMOVED***1 slot through the repair ladder with a controlled
    probe; returns (records, repairs, replan_called)."""
    from semantic_search.services import research_service as rs
    from semantic_search.services.query_parser_service import (
        QueryParserService,
    )
    from semantic_search.services.query_executor_service import (
        QueryExecutorService,
    )

    records = [{
        "question": "Which restaurants are within 5km of downtown X?",
        "slot": "eat", "template": "FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
        "result_count": 0,
    }]
    replan_called = []

    class FakeLLM:
        @staticmethod
        def chat_json(messages, temperature=0.0, max_tokens=300, **kw):
            return {"question": "rewritten"}

    monkeypatch.setattr(rs, "_PRESENCE_GATE_ENABLED", presence_gate)
    monkeypatch.setattr(rs, "_REPLAN_LLM_ENABLED", True)
    monkeypatch.setattr(
        QueryParserService, "get_instance",
        staticmethod(lambda: type("P", (), {
            "parse": staticmethod(
                lambda q: {"template": "PLACE-ATTRIBUTE-QUERY (***REMOVED***8)"},
            ),
        })()),
    )
    monkeypatch.setattr(
        QueryExecutorService, "execute",
        staticmethod(lambda *a, **kw: {"results": list(probe_rows)}),
    )
    monkeypatch.setattr(
        ResearchOrchestratorService, "_class_swap_question",
        classmethod(lambda cls, *a, **kw: None),
    )
    monkeypatch.setattr(
        ResearchOrchestratorService, "_llm_replan",
        classmethod(lambda cls, *a, **kw: replan_called.append(1) or "rewritten"),
    )
    monkeypatch.setattr(
        ResearchOrchestratorService, "_rerun",
        classmethod(lambda cls, *a, **kw: False),
    )
    repairs = ResearchOrchestratorService._repair_empty_slots(
        records, FakeLLM(), "IE", None, None, recipe=FOOD_REPORT,
    )
    return records, repairs, bool(replan_called)


def test_presence_gate_skips_replan_when_absent(monkeypatch):
    ***REMOVED*** Dense probe, zero class matches: the posterior says absent, the
    ***REMOVED*** LLM replan is skipped, the evidence fallback records the digest.
    probe = [{"tags": {"amenity": t}, "name": f"p{i}"}
             for i, t in enumerate(["cafe"] * 30)]
    records, repairs, replan_called = _run_repair(
        monkeypatch, probe, presence_gate=True,
    )
    assert replan_called is False
    assert records[0]["repair"] == "fallback_evidence"
    assert records[0]["presence"]["decision"] == "absent"
    assert repairs[0]["presence"] == "absent"


def test_presence_gate_off_keeps_replan(monkeypatch):
    probe = [{"tags": {"amenity": t}, "name": f"p{i}"}
             for i, t in enumerate(["cafe"] * 30)]
    records, repairs, replan_called = _run_repair(
        monkeypatch, probe, presence_gate=False,
    )
    assert replan_called is True
    assert "presence" not in records[0]


def test_presence_gate_uncertain_still_replans(monkeypatch):
    ***REMOVED*** Sparse probe: the posterior is uncertain, the replan runs.
    probe = [{"tags": {"amenity": "cafe"}, "name": "p0"}]
    records, repairs, replan_called = _run_repair(
        monkeypatch, probe, presence_gate=True,
    )
    assert replan_called is True
    assert records[0]["presence"]["decision"] == "uncertain"


def test_presence_gate_flag_defaults_off():
    ***REMOVED*** The module default (no env set in the test process): the repair
    ***REMOVED*** flow is unchanged unless RESEARCH_PRESENCE_GATE is enabled.
    assert _PRESENCE_GATE_ENABLED is False
