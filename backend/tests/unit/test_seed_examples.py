"""
Tests for research seed-examples selection (ISSUE-50 Part 1).

The greeting's example seeds must be food venues (cuisine-tagged, the
GIN-indexable food marker), embedding-bearing (a suggested seed can
never fail the run with "seed entity has no tag embedding"), and
ordered by tag richness before the deterministic md5 tiebreak.

Mock-only: asserts the queryset chain the view builds, no DB required.
"""

import json
import pytest
from unittest import mock

from worldkg_nca.models import OsmEntity
from worldkg_nca.views import research as research_views


class _FakeQS:
    """Records the queryset chain; returns no rows."""

    def __init__(self):
        self.calls = []

    def filter(self, **kw):
        self.calls.append(("filter", kw))
        return self

    def exclude(self, **kw):
        self.calls.append(("exclude", kw))
        return self

    def annotate(self, **kw):
        self.calls.append(("annotate", kw))
        return self

    def order_by(self, *args):
        self.calls.append(("order_by", args))
        return self

    def __getitem__(self, _slice):
        return []


def _request(country="JM", snapshot="2025_12_31"):
    return type("R", (), {"method": "GET", "GET": {
        "country_code": country, "snapshot_date": snapshot,
    }})()


def _body(resp):
    return json.loads(resp.content.decode())


@pytest.fixture
def fake_qs():
    fake = _FakeQS()
    with mock.patch.object(
        OsmEntity.objects, "using", return_value=fake
    ), mock.patch(
        "worldkg_nca.views.research.resolve_iso_code", return_value="JM"
    ), mock.patch(
        ***REMOVED*** No boundary row: the geometry guard is skipped, current
        ***REMOVED*** behavior preserved (hermetic - no default-DB access).
        "worldkg_nca.views.research._country_bbox", return_value=None
    ):
        yield fake


def test_seed_examples_selects_cuisine_food_venues(fake_qs):
    resp = research_views.research_seed_examples(_request())
    assert resp.status_code == 200
    assert _body(resp) == {"examples": []}

    filters = [kw for kind, kw in fake_qs.calls if kind == "filter"]
    has_keys = [kw.get("tags__has_key") for kw in filters]
    assert "name" in has_keys
    assert "cuisine" in has_keys
    assert "amenity" not in (has_keys or [])  ***REMOVED*** no venue-class shotgun


def test_seed_examples_requires_embeddings_and_coordinates(fake_qs):
    research_views.research_seed_examples(_request())
    excludes = [kw for kind, kw in fake_qs.calls if kind == "exclude"]
    keys = {k for kw in excludes for k in kw}
    assert "gv_tags_embedding__isnull" in keys
    assert "geom__isnull" in keys


def test_seed_examples_orders_by_richness_then_md5(fake_qs):
    research_views.research_seed_examples(_request())
    order = [args for kind, args in fake_qs.calls if kind == "order_by"]
    assert order, "order_by must be applied"
    joined = " ".join(str(o) for o in order[0])
    assert "tag_count" in joined
    assert "md5" in joined


def test_seed_examples_uses_vectors_db(fake_qs):
    research_views.research_seed_examples(_request())
    assert fake_qs.calls  ***REMOVED*** the chain was exercised on the vectors manager


def test_seed_examples_bbox_guard_applies_when_boundary_exists():
    fake = _FakeQS()
    with mock.patch.object(
        OsmEntity.objects, "using", return_value=fake
    ), mock.patch(
        "worldkg_nca.views.research.resolve_iso_code", return_value="JM"
    ), mock.patch(
        "worldkg_nca.views.research._country_bbox",
        return_value=[-78.5, 17.5, -76.0, 18.7],
    ):
        resp = research_views.research_seed_examples(_request())
    assert resp.status_code == 200
    filters = [kw for kind, kw in fake.calls if kind == "filter"]
    assert any("geom__within" in kw for kw in filters)