import pytest

from extraction import models as extraction_models
from extraction.services.osm_wikidata_resolver import (
    resolve_country_bbox,
    ISO_BBOX_FALLBACK,
    parse_poly_bbox,
)


def test_resolve_country_bbox_uses_static_fallback_for_known_iso_codes(monkeypatch):
    """Ensure resolve_country_bbox returns expected bbox for sample ISO codes.

    This test exercises the fallback path (no OsmBoundary / PolygonFile rows)
    and verifies that the unified resolver matches the static table for a
    representative subset of countries.
    """

    # Avoid touching the real database by stubbing out the models used
    # inside resolve_country_bbox so that they never hit a live
    # connection. This lets us exercise the fallback path against the
    # unified ISO_BBOX_FALLBACK table.

    class _DummyQS:
        def filter(self, *args, **kwargs):  # pragma: no cover - trivial
            return self

        def first(self):  # pragma: no cover - trivial
            return None

    class _DummyManager:
        def filter(self, *args, **kwargs):  # pragma: no cover - trivial
            return _DummyQS()

    class _DummyModel:
        objects = _DummyManager()

    # Patch both OsmBoundary and PolygonFile to our dummy model so all
    # ORM lookups return None and the resolver is forced to use the
    # static fallback table.
    monkeypatch.setattr(extraction_models, "OsmBoundary", _DummyModel, raising=False)
    monkeypatch.setattr(extraction_models, "PolygonFile", _DummyModel, raising=False)

    sample_codes = [
        "DE",  # Germany
        "GB",  # United Kingdom
        "US",  # United States
        "CA",  # Canada
        "JM",  # Jamaica (Caribbean)
        "MZ",  # Mozambique (extended set)
    ]

    for code in sample_codes:
        expected = ISO_BBOX_FALLBACK[code]
        bbox = resolve_country_bbox(code)
        assert bbox is not None, f"No bbox resolved for {code}"
        # Compare element-wise with a small float tolerance
        for got, exp in zip(bbox, expected):
            assert got == pytest.approx(exp, rel=1e-6, abs=1e-6)


def test_parse_poly_bbox_basic(tmp_path):
    """parse_poly_bbox should compute the min/max lon/lat from a simple .poly file."""

    poly_path = tmp_path / "test.poly"
    # Minimal valid .poly structure: name, ring id, coordinates, END, END
    poly_contents = """test_polygon
1
10 20
30 40
END
END
"""
    poly_path.write_text(poly_contents)

    bbox = parse_poly_bbox(str(poly_path))
    assert bbox is not None
    min_lon, min_lat, max_lon, max_lat = bbox
    assert min_lon == pytest.approx(10.0)
    assert min_lat == pytest.approx(20.0)
    assert max_lon == pytest.approx(30.0)
    assert max_lat == pytest.approx(40.0)
