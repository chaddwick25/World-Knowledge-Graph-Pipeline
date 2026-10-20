"""Unit tests for the query-time re-rank (GeoUslpMixin).

Regression: "Island Grill within 150km of X" ranked a nearer "cook shop"
and name-less traffic islands above the Island Grill entities, because
the FILTER-AGGREGATE-MEASURE pool was sorted by distance and the re-rank
never ran on that path. Fix: when the OBJECT concept is a proper name
(not a resolvable amenity category), align it against each entity's
`name` field via FastText cosine (name_score) and include it in
combined_score.

The FastText embedder is stubbed with a deterministic token-presence
embedding (cosine = token-overlap), no model required.
"""

import re

import numpy as np
import pytest

from semantic_search.services.fasttext_service import FastTextEmbeddingService
from semantic_search.services.query_executor_service.geo_uslp import GeoUslpMixin
from semantic_search.services.query_executor_service.template_executors import (
    TemplateExecutorsMixin,
)


# ── Deterministic embedder stub ─────────────────────────────────────────

_TOKEN_IDS = {
    "island": 0, "grill": 1, "cook": 2, "shop": 3, "auhsaj": 4, "chill": 5,
    "santos": 6, "bar": 7, "cafe": 8, "sea": 9, "crab": 10,
}


def _fake_text_embedding(text):
    """Token-presence vector, L2-normalized: cosine = token-overlap."""
    tokens = sorted({t for t in re.findall(r"[a-z]+", (text or "").lower())})
    vec = np.zeros(300, dtype=np.float32)
    for tok in tokens:
        idx = _TOKEN_IDS.get(tok)
        if idx is not None:
            vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    monkeypatch.setattr(
        FastTextEmbeddingService, "calculate_text_embedding",
        staticmethod(_fake_text_embedding),
    )


@pytest.fixture(autouse=True)
def _stub_uslp(monkeypatch):
    monkeypatch.setattr(
        GeoUslpMixin, "_get_uslp_predicted_tails",
        classmethod(lambda cls, *a, **k: set()),
    )


# Anchor: Ocho Rios High School (the JM repro).
_ANCHOR = {"lat": 18.4018432, "lon": -77.1031874, "osm_id": 4835797386}

_TEMPLATE = "FILTER-AGGREGATE-MEASURE (#1)"


def _result(osm_id, name, lat, lon):
    return {"osm_id": osm_id, "name": name, "lat": lat, "lon": lon}


# ── Re-rank behavior ─────────────────────────────────────────────────────

def test_name_query_ranks_exact_name_first():
    """The Island Grill repro: exact-name entity must beat the nearer
    non-matching entity and the name-less traffic island."""
    results = [
        _result(5161922028, "cook shop", 18.419609, -77.160181),
        _result(5161897111, "", 18.4600232, -77.3499252),
        _result(4691505846, "Island Grill", 18.0519093, -76.792293),
    ]
    out = GeoUslpMixin._enrich_with_geo_and_uslp(
        results, _TEMPLATE, _ANCHOR, 150000, _ANCHOR["osm_id"],
        "JM", None, [], name_query="island grill",
    )

    assert out[0]["osm_id"] == 4691505846, (
        f"expected Island Grill first, got {out[0]['name']}"
    )
    assert out[0]["combined_score"] > out[1]["combined_score"] > out[2]["combined_score"]


def test_name_score_exact_match_and_nameless():
    results = [
        _result(1, "Island Grill", 18.05, -76.79),
        _result(2, "", 18.46, -77.35),
    ]
    out = GeoUslpMixin._enrich_with_geo_and_uslp(
        results, _TEMPLATE, _ANCHOR, 150000, None, "JM", None, [],
        name_query="island grill",
    )

    by_id = {r["osm_id"]: r for r in out}
    # "island grill" vs "Island Grill" — same token bag → cosine 1.0
    assert by_id[1]["name_score"] == pytest.approx(1.0, abs=1e-4)
    assert by_id[2]["name_score"] == 0.0


def test_no_name_query_leaves_behavior_unchanged():
    """Amenity-category queries (name_query=None) get no name_score and
    combined_score stays geo + uslp."""
    results = [
        _result(1, "Cafe A", 18.40, -77.10),
        _result(2, "Cafe B", 18.41, -77.11),
    ]
    out = GeoUslpMixin._enrich_with_geo_and_uslp(
        results, _TEMPLATE, _ANCHOR, 150000, None, "JM", None, [],
        name_query=None,
    )

    for r in out:
        assert "name_score" not in r
        assert r["combined_score"] == pytest.approx(r["geo_score"], abs=1e-4)


def test_combined_score_composition():
    results = [_result(1, "Island Grill", 18.05, -76.79)]
    out = GeoUslpMixin._enrich_with_geo_and_uslp(
        results, _TEMPLATE, _ANCHOR, 150000, None, "JM", None, [],
        name_query="island grill",
    )
    r = out[0]
    expected = round(r.get("diffusion_score", 0.0) + r["name_score"]
                     + r["geo_score"] + r["uslp_boost"], 6)
    assert r["combined_score"] == expected


# ── Handler wiring ───────────────────────────────────────────────────────

def test_rerank_with_name_alignment_proper_name(monkeypatch):
    """A non-resolvable OBJECT text ("island grill") becomes name_query."""
    monkeypatch.setattr(
        TemplateExecutorsMixin, "_resolve_amenity_tag",
        classmethod(lambda cls, *a, **k: None),
    )
    captured = {}

    def fake_enrich(cls, results, template, anchor_coords, radius_m,
                    anchor_osm_id, country_code, snapshot_date, trace,
                    name_query=None):
        captured["name_query"] = name_query
        captured["template"] = template
        captured["anchor_osm_id"] = anchor_osm_id
        return results

    monkeypatch.setattr(
        TemplateExecutorsMixin, "_enrich_with_geo_and_uslp",
        classmethod(fake_enrich), raising=False,
    )

    results = [_result(1, "Island Grill", 18.05, -76.79)]
    out = TemplateExecutorsMixin._rerank_with_name_alignment(
        results, "island grill", _ANCHOR, 150000, "JM", None, [],
    )

    assert out is results
    assert captured["name_query"] == "island grill"
    assert captured["template"] == _TEMPLATE
    assert captured["anchor_osm_id"] == _ANCHOR["osm_id"]


def test_rerank_with_name_alignment_category(monkeypatch):
    """A resolvable amenity category ("cafes") gets no name_query."""
    monkeypatch.setattr(
        TemplateExecutorsMixin, "_resolve_amenity_tag",
        classmethod(lambda cls, *a, **k: "cafe"),
    )
    captured = {}

    def fake_enrich(cls, results, template, anchor_coords, radius_m,
                    anchor_osm_id, country_code, snapshot_date, trace,
                    name_query=None):
        captured["name_query"] = name_query
        return results

    monkeypatch.setattr(
        TemplateExecutorsMixin, "_enrich_with_geo_and_uslp",
        classmethod(fake_enrich), raising=False,
    )

    TemplateExecutorsMixin._rerank_with_name_alignment(
        [_result(1, "Cafe A", 18.40, -77.10)], "cafes",
        _ANCHOR, 150000, "JM", None, [],
    )

    assert captured["name_query"] is None
