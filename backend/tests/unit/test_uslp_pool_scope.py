"""Regression tests for USLP candidate-pool scoping and empty-pool reporting.

Pins the 2026-11-11 USLP pool fix:

1. ``load_candidate_pool_from_db`` scopes the pool with the ``country_code``
   partition key, never with OSM tags (``addr:country`` / ``is_in:country`` /
   ``is_in``). The tag filter cut JM @ 2025_12_31 from 1,113,365 geometry rows
   to 384, and the in-chain run wrote nothing.
2. Country and polygon are independent filters: passing a polygon no longer
   disables the country filter (and vice versa).
3. An empty pool is reported at ERROR with its country/snapshot scope, so a
   96-row pool and a 27,151-row pool can no longer look identical from outside.
4. ``TorchUSLP.predict_links_batched`` keeps the top-k overall and lets
   ``persist_links`` split accepted from rejected, so the rejected table is
   populated on the GPU path too.

No database and no GPU are required: the queryset is a recording fake and the
scoring service is exercised on the CPU device.
"""

import logging
from types import SimpleNamespace

import pytest
import torch
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError

from igea.services.gpu_uslp_service import GPUAcceleratedUSLP
from igea.services.spatial_link_prediction import SpatialLinkPredictionService
from igea.services.torch_uslp_service import TorchUSLP


# The pipeline routes USLP to the k80 worker, so on that host these tests
# instantiate the services on cuda:0 and exercise the real GPU path. On a
# CPU-only checkout they fall back to cpu and still cover the logic.
DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"


class _StubGPUFastText:
    """Minimal GPU FastText stand-in: embeddings on the service device."""

    def __init__(self, device):
        self.device = device

    def calculate_embedding_batch(self, texts):
        return torch.randn(len(texts), 300, device=self.device)

    def cosine_similarity_batch(self, a, b):
        a = torch.nn.functional.normalize(a, dim=1)
        b = torch.nn.functional.normalize(b, dim=1)
        return (a * b).sum(dim=1)


class _StubGPUHaversine:
    """Minimal GPU haversine stand-in: every candidate is in radius."""

    def __init__(self, device):
        self.device = device

    def filter_by_radius(self, head_lat, head_lon, candidate_lats,
                         candidate_lons, radius_km):
        n = len(candidate_lats)
        return (
            torch.arange(n, device=self.device),
            torch.zeros(n, device=self.device),
        )


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _RecordingQuerySet:
    """QuerySet stand-in that records filter kwargs and yields fixed rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.filters = []
        self.limit = None

    def filter(self, *args, **kwargs):
        self.filters.append(kwargs)
        return self

    def __getitem__(self, item):
        self.limit = item
        return self

    def iterator(self, chunk_size=1_000):
        return iter(self.rows)

    def filter_keys(self):
        return {key for kwargs in self.filters for key in kwargs}


class _FakeManager:
    def __init__(self, qs):
        self.qs = qs
        self.alias = None

    def using(self, alias):
        self.alias = alias
        return self

    def filter(self, *args, **kwargs):
        self.qs.filter(*args, **kwargs)
        return self.qs


def _entity(osm_id=1, lat=17.99, lon=-76.79, tags=None):
    return SimpleNamespace(
        osm_id=osm_id,
        geom=SimpleNamespace(y=lat, x=lon),
        tags=tags if tags is not None else {"name": "Kingston", "place": "city"},
        wkg_class=None,
        gv_tags_embedding=None,
    )


def _load_with_fake_model(service, rows=(), **kwargs):
    qs = _RecordingQuerySet(rows)
    fake_model = SimpleNamespace(objects=_FakeManager(qs))
    with patch("worldkg_nca.models.OsmEntity", fake_model):
        size = service.load_candidate_pool_from_db(**kwargs)
    return size, qs


# --------------------------------------------------------------------------- #
# Pool scoping
# --------------------------------------------------------------------------- #

class TestPoolScoping:

    def test_pool_scopes_by_country_code_partition_key(self):
        service = SpatialLinkPredictionService()
        size, qs = _load_with_fake_model(
            service, country="JM", snapshot_id="2025_12_31",
        )

        assert size == 0
        assert {"country_code": "JM"} in qs.filters
        assert {"snapshot_id": "2025_12_31"} in qs.filters
        assert "tags" not in qs.filter_keys()

    def test_pool_keeps_country_filter_when_polygon_is_given(self):
        service = SpatialLinkPredictionService()
        polygon = "POLYGON((-77.5 17.5, -76.0 17.5, -76.0 18.5, -77.5 18.5, -77.5 17.5))"

        _, qs = _load_with_fake_model(
            service, country="JM", polygon_wkt=polygon, snapshot_id="2025_12_31",
        )

        keys = qs.filter_keys()
        assert "country_code" in keys
        assert "geom__within" in keys
        assert "tags" not in keys

    def test_entity_without_addr_country_tag_stays_in_pool(self):
        """The regression: addr:country is a rare address tag, not a country
        assignment. An entity with no country tags must not be dropped."""
        service = SpatialLinkPredictionService()
        row = _entity(tags={"name": "Kingston", "place": "city"})

        size, _ = _load_with_fake_model(
            service, rows=[row], country="JM", snapshot_id="2025_12_31",
        )

        assert size == 1
        assert service._pool[0]["osm_id"] == row.osm_id

    def test_pool_logs_size_and_scope(self, caplog):
        service = SpatialLinkPredictionService()
        caplog.set_level(logging.INFO, logger="igea.services.spatial_link_prediction")

        _load_with_fake_model(
            service, rows=[_entity()], country="JM", snapshot_id="2025_12_31",
        )

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "USLP: Pool: 1 candidates" in m
            and "country_code=JM" in m
            and "snapshot=2025_12_31" in m
            for m in messages
        )

    def test_unresolvable_country_warns_and_still_scopes(self, caplog):
        service = SpatialLinkPredictionService()
        caplog.set_level(logging.WARNING, logger="igea.services.spatial_link_prediction")

        with patch(
            "core.services.planet_init.osm_wikidata_resolver.resolve_iso_code",
            return_value="",
        ):
            _, qs = _load_with_fake_model(service, country="Jamaica")

        assert {"country_code": "JAMAICA"} in qs.filters
        assert any("Could not resolve country" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Empty pool reporting
# --------------------------------------------------------------------------- #

class TestEmptyPoolReporting:

    def test_cpu_empty_pool_logs_error_with_scope(self, caplog):
        service = SpatialLinkPredictionService()
        service._pool_country_code = "JM"
        service._pool_snapshot_id = "2025_12_31"
        caplog.set_level(logging.ERROR, logger="igea.services.spatial_link_prediction")

        links = service.predict_links_for_entity(
            head_osm_id=1, head_lat=18.0, head_lon=-76.8,
            head_tags={"addr:city": "Kingston"},
        )

        assert links == []
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "candidate pool is empty" in m
            and "country_code=JM" in m
            and "snapshot=2025_12_31" in m
            for m in errors
        )

    def test_gpu_empty_pool_logs_error_with_scope(self, caplog):
        service = GPUAcceleratedUSLP(device=DEVICE)
        service._pool_country_code = "JM"
        service._pool_snapshot_id = "2025_12_31"
        caplog.set_level(logging.ERROR, logger="igea.services.gpu_uslp_service")

        links = service.predict_links_for_entity(
            head_osm_id=1, head_lat=18.0, head_lon=-76.8,
            head_tags={"addr:city": "Kingston"},
        )

        assert links == []
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "GPUAcceleratedUSLP.predict_links_for_entity" in m
            and "candidate pool is empty" in m
            and "country_code=JM" in m
            for m in errors
        )

    def test_torch_empty_pool_logs_error_with_scope(self, caplog):
        service = TorchUSLP(device=DEVICE)
        service._pool_country_code = "JM"
        service._pool_snapshot_id = "2025_12_31"
        caplog.set_level(logging.ERROR, logger="igea.services.torch_uslp_service")

        links = service.predict_links_batched(
            [{"osm_id": 1, "lat": 18.0, "lon": -76.8,
              "tags": {"addr:city": "Kingston"}}],
        )

        assert links == []
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "TorchUSLP.predict_links_batched" in m
            and "candidate pool is empty" in m
            and "country_code=JM" in m
            for m in errors
        )


# --------------------------------------------------------------------------- #
# GPU path keeps rejected candidates
# --------------------------------------------------------------------------- #

class TestTorchBatchedKeepsRejected:

    def test_below_threshold_links_are_returned_for_the_rejected_table(self):
        service = TorchUSLP(device=DEVICE)
        service._pool = [
            {"osm_id": 10, "lat": 18.0, "lon": -76.8, "tags": {}, "wkg_class": None},
            {"osm_id": 11, "lat": 18.1, "lon": -76.7, "tags": {}, "wkg_class": None},
            {"osm_id": 12, "lat": 18.2, "lon": -76.6, "tags": {}, "wkg_class": None},
        ]
        service.gpu_fasttext = Mock()
        service.gpu_fasttext.calculate_embedding_batch.return_value = torch.zeros(1, 300)

        scores = torch.tensor([[0.9, 0.2, 0.1]])
        components = torch.tensor([[0.9, 0.2, 0.1]])
        service.score_batch = Mock(
            return_value=(scores, components, components, components)
        )

        heads = [{"osm_id": 1, "lat": 18.0, "lon": -76.8,
                  "tags": {"addr:city": "Kingston"}}]
        links = service.predict_links_batched(heads, threshold=0.7, top_k=2)

        assert len(links) == 2
        assert links[0]["normalized_score"] == pytest.approx(0.9)
        assert links[1]["normalized_score"] == pytest.approx(0.2)
        assert [link["tail_osm_id"] for link in links] == [10, 11]

    def test_accepted_set_is_unchanged_by_keeping_top_k_overall(self):
        """Above-threshold links in top-k overall == top-k above threshold."""
        service = TorchUSLP(device=DEVICE)
        service._pool = [
            {"osm_id": i, "lat": 18.0, "lon": -76.8, "tags": {}, "wkg_class": None}
            for i in range(1, 6)
        ]
        service.gpu_fasttext = Mock()
        service.gpu_fasttext.calculate_embedding_batch.return_value = torch.zeros(1, 300)

        scores = torch.tensor([[0.95, 0.80, 0.75, 0.40, 0.10]])
        components = torch.tensor([[0.95, 0.80, 0.75, 0.40, 0.10]])
        service.score_batch = Mock(
            return_value=(scores, components, components, components)
        )

        heads = [{"osm_id": 1, "lat": 18.0, "lon": -76.8,
                  "tags": {"addr:city": "Kingston"}}]
        links = service.predict_links_batched(heads, threshold=0.7, top_k=3)

        accepted = [l for l in links if l["normalized_score"] >= 0.7]
        assert [l["tail_osm_id"] for l in accepted] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# GPU preflight: USLP must not fall back to CPU silently
# --------------------------------------------------------------------------- #

class TestGPUFailurePolicy:

    def test_gpu_request_without_cuda_fails_the_step(self, monkeypatch):
        """A USLP task that lands on a host without CUDA must fail, not score
        on CPU. The CPU and GPU paths previously produced different counts."""
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
        )
        monkeypatch.setattr(
            "igea.management.commands.predict_spatial_links._lazy_import_torch",
            lambda: fake_torch,
        )

        with pytest.raises(CommandError, match="CUDA is not available"):
            call_command("predict_spatial_links", "--gpu", "--limit", "1")


# --------------------------------------------------------------------------- #
# K80 GPU path (runs on the k80 worker; skipped elsewhere)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not torch.cuda.is_available(),
    reason="requires CUDA (k80 worker)",
)
class TestK80GPUPath:

    def test_gpu_service_binds_to_cuda(self):
        service = GPUAcceleratedUSLP(device="cuda:0")

        assert service.device.type == "cuda"
        assert torch.cuda.get_device_name(service.device)

    def test_predict_links_for_entity_scores_on_the_gpu(self):
        service = GPUAcceleratedUSLP(device="cuda:0")
        service._pool = [
            {"osm_id": 10, "lat": 18.00, "lon": -76.80,
             "tags": {"name": "Kingston"}, "wkg_class": "wkgs:City"},
            {"osm_id": 11, "lat": 18.01, "lon": -76.81,
             "tags": {"name": "Spanish Town"}, "wkg_class": "wkgs:Town"},
            {"osm_id": 12, "lat": 18.02, "lon": -76.82,
             "tags": {"name": "Portmore"}, "wkg_class": "wkgs:City"},
        ]
        coords = torch.tensor(
            [[e["lat"], e["lon"]] for e in service._pool], device=service.device,
        )
        service._gpu_pool_coords = coords
        service._gpu_pool_center_coords = {4: coords}
        service._gpu_pool_d_max = {
            4: torch.full((3,), 100.0, device=service.device),
        }
        service._gpu_pool_embeddings = torch.randn(3, 300, device=service.device)
        service._gpu_pool_class_embeddings = torch.randn(3, 300, device=service.device)
        service.gpu_fasttext = _StubGPUFastText(service.device)
        service.gpu_haversine = _StubGPUHaversine(service.device)

        links = service.predict_links_for_entity(
            head_osm_id=999, head_lat=18.0, head_lon=-76.8,
            head_tags={"addr:city": "Kingston"}, threshold=0.0, top_k=2,
        )

        assert service.device.type == "cuda"
        assert len(links) == 2
        assert all(link["tail_osm_id"] in (10, 11, 12) for link in links)

    def test_torch_batched_keeps_rejected_on_the_gpu(self):
        service = TorchUSLP(device="cuda:0")
        service._pool = [
            {"osm_id": 10, "lat": 18.0, "lon": -76.8, "tags": {}, "wkg_class": None},
            {"osm_id": 11, "lat": 18.1, "lon": -76.7, "tags": {}, "wkg_class": None},
            {"osm_id": 12, "lat": 18.2, "lon": -76.6, "tags": {}, "wkg_class": None},
        ]
        service.gpu_fasttext = Mock()
        service.gpu_fasttext.calculate_embedding_batch.return_value = torch.zeros(
            1, 300, device=service.device,
        )

        scores = torch.tensor([[0.9, 0.2, 0.1]], device=service.device)
        service.score_batch = Mock(
            return_value=(scores, scores, scores, scores)
        )

        heads = [{"osm_id": 1, "lat": 18.0, "lon": -76.8,
                  "tags": {"addr:city": "Kingston"}}]
        links = service.predict_links_batched(heads, threshold=0.7, top_k=2)

        assert service.device.type == "cuda"
        assert [link["normalized_score"] for link in links] == [0.9, 0.2]
