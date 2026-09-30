from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from worldkg_nca.models import OsmEntity, PrecomputedLinkCandidate


@override_settings(ENABLE_PRECOMPUTED_LINK_CANDIDATES=True)
class PrecomputedLinkCandidatesAPITests(TestCase):
    databases = {"default", "vectors"}

    def setUp(self):
        self.entity = OsmEntity.objects.using("vectors").create(
            osm_type="node",
            osm_id=123,
            tags={"name": "Test", "amenity": "cafe"},
        )

    @mock.patch("worldkg_nca.views.WorldKGLinkCandidateService")
    def test_lazy_cache_write_on_miss(self, svc_cls):
        svc = svc_cls.return_value
        svc._get_projection_weights.return_value = (1.0, 1.0, 1.0, {"asset_id": "11111111-1111-1111-1111-111111111111"})
        svc.get_alignment_candidates.return_value = {
            "projection_weights": {
                "w_geo": 1.0,
                "w_name": 1.0,
                "w_class": 1.0,
                "asset_id": "11111111-1111-1111-1111-111111111111",
            },
            "candidates": [
                {
                    "wikidata_id": "Q1",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q1",
                    "label": "Test",
                    "scores": {"final_score": 0.9},
                }
            ],
        }

        url = reverse("nca_link_candidates")
        resp = self.client.post(
            url,
            {"osm_type": "node", "osm_id": self.entity.osm_id, "country_code": "JM", "top_k": 5},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("candidates", data)
        self.assertEqual(len(data["candidates"]), 1)

        pre = PrecomputedLinkCandidate.objects.using("vectors").filter(
            osm_type="node",
            osm_id=self.entity.osm_id,
            projection_asset_id="11111111-1111-1111-1111-111111111111",
        ).first()
        self.assertIsNotNone(pre)
        self.assertEqual(len(pre.candidates), 1)

    @mock.patch("worldkg_nca.views.WorldKGLinkCandidateService")
    def test_precomputed_hit_skips_service_get_alignment(self, svc_cls):
        svc = svc_cls.return_value
        svc._get_projection_weights.return_value = (1.0, 1.0, 1.0, {"asset_id": "22222222-2222-2222-2222-222222222222"})

        PrecomputedLinkCandidate.objects.using("vectors").create(
            country_code="JM",
            osm_type="node",
            osm_id=self.entity.osm_id,
            projection_asset_id="22222222-2222-2222-2222-222222222222",
            candidates=[
                {
                    "wikidata_id": "Q2",
                    "wikidata_uri": "http://www.wikidata.org/entity/Q2",
                    "label": "Cached",
                    "scores": {"final_score": 0.8},
                }
            ],
        )

        url = reverse("nca_link_candidates")
        resp = self.client.post(
            url,
            {"osm_type": "node", "osm_id": self.entity.osm_id, "country_code": "JM", "top_k": 5},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["wikidata_id"], "Q2")

        # get_alignment_candidates should not have been called on a cache hit
        svc.get_alignment_candidates.assert_not_called()
