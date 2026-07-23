"""
Test to compare Triple-Space vs ANN Search Endpoints

This test compares the performance and results of:
1. Triple-space search (use_ann=false) - uses GV-Tags 300D with separate scoring
2. ANN search (use_ann=true) - uses 400D static_embedding with HNSW index

Usage:
    cd backend
    poetry run python manage.py test tests.test_semantic_search_comparison

Note: This test uses the actual 'vectors' database, not the test database.
"""

import time
from django.test import TestCase
from rest_framework.test import APIClient
from django.contrib.gis.geos import Point
from worldkg_nca.models import OsmEntity
from extraction.services.osm_wikidata_resolver import resolve_country_bbox
from django.test.utils import override_settings
from unittest.mock import patch


class SemanticSearchComparisonTest(TestCase):
    """Compare triple-space vs ANN search performance and results"""

    databases = {'vectors'}  # Use actual vectors database, not test database

    def setUp(self):
        self.client = APIClient()
        self.country_code = "monaco"
        self.query_tags = {"amenity": "restaurant"}
        self.top_k = 20
        
        # Mock bbox resolution to avoid database dependency
        self.bbox_patch = patch('extraction.services.osm_wikidata_resolver.resolve_country_bbox')
        self.mock_bbox = self.bbox_patch.start()
        self.mock_bbox.return_value = (7.4, 43.7, 7.45, 43.75)  # Monaco bbox

    def tearDown(self):
        self.bbox_patch.stop()

    def test_triple_space_search(self):
        """Test triple-space search endpoint"""
        response = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": False,
            },
            format='json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data['country_code'], self.country_code)
        self.assertEqual(data['top_k'], self.top_k)
        self.assertEqual(data['search_mode'], 'triple_space')
        self.assertIsInstance(data['count'], int)
        self.assertIsInstance(data['results'], list)
        
        # Validate result structure
        if data['count'] > 0:
            result = data['results'][0]
            self.assertIn('osm_type', result)
            self.assertIn('osm_id', result)
            self.assertIn('tags', result)
            self.assertIn('scores', result)
            self.assertIn('name_score', result['scores'])
            self.assertIn('geo_score', result['scores'])
            self.assertIn('class_score', result['scores'])
            self.assertIn('final_score', result['scores'])
            self.assertNotIn('ann_distance', result['scores'])

        print(f"\nTriple-space search: {data['count']} results")

    def test_ann_search(self):
        """Test ANN search endpoint"""
        response = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": True,
                "name_distance_threshold": 2.0,
            },
            format='json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data['country_code'], self.country_code)
        self.assertEqual(data['top_k'], self.top_k)
        self.assertEqual(data['search_mode'], 'ann')
        self.assertIsInstance(data['count'], int)
        self.assertIsInstance(data['results'], list)
        
        # Validate result structure
        if data['count'] > 0:
            result = data['results'][0]
            self.assertIn('osm_type', result)
            self.assertIn('osm_id', result)
            self.assertIn('tags', result)
            self.assertIn('scores', result)
            self.assertIn('name_score', result['scores'])
            self.assertIn('geo_score', result['scores'])
            self.assertIn('class_score', result['scores'])
            self.assertIn('final_score', result['scores'])
            self.assertIn('ann_distance', result['scores'])

        print(f"\nANN search: {data['count']} results")

    def test_performance_comparison(self):
        """Compare performance between triple-space and ANN search"""
        # Triple-space search
        start_time = time.time()
        response_triple = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": False,
            },
            format='json'
        )
        triple_time = time.time() - start_time
        
        # ANN search
        start_time = time.time()
        response_ann = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": True,
                "name_distance_threshold": 2.0,
            },
            format='json'
        )
        ann_time = time.time() - start_time
        
        data_triple = response_triple.json()
        data_ann = response_ann.json()
        
        print(f"\n{'='*60}")
        print(f"Performance Comparison: {self.country_code}")
        print(f"{'='*60}")
        print(f"Triple-space: {data_triple['count']} results in {triple_time:.3f}s")
        print(f"ANN:          {data_ann['count']} results in {ann_time:.3f}s")
        print(f"Speedup:      {triple_time/ann_time:.2f}x")
        print(f"{'='*60}")
        
        # Both should succeed
        self.assertEqual(response_triple.status_code, 200)
        self.assertEqual(response_ann.status_code, 200)

    def test_with_geographic_scoring(self):
        """Test both endpoints with lat/lon for geographic scoring"""
        lat, lon = 43.7334, 7.4214
        
        # Triple-space with geo
        response_triple = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "lat": lat,
                "lon": lon,
                "top_k": self.top_k,
                "use_ann": False,
            },
            format='json'
        )
        
        # ANN with geo
        response_ann = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "lat": lat,
                "lon": lon,
                "top_k": self.top_k,
                "use_ann": True,
                "name_distance_threshold": 2.0,
            },
            format='json'
        )
        
        data_triple = response_triple.json()
        data_ann = response_ann.json()
        
        # Both should have geo_score > 0
        if data_triple['count'] > 0:
            self.assertGreater(data_triple['results'][0]['scores']['geo_score'], 0)
        
        if data_ann['count'] > 0:
            self.assertGreater(data_ann['results'][0]['scores']['geo_score'], 0)
        
        print(f"\nWith geo scoring:")
        print(f"Triple-space: {data_triple['count']} results")
        print(f"ANN:          {data_ann['count']} results")

    def test_with_exact_tag_match(self):
        """Test both endpoints with exact tag matching"""
        # Triple-space with exact match
        response_triple = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "exact_tag_match": True,
                "use_ann": False,
            },
            format='json'
        )
        
        # ANN with exact match
        response_ann = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "exact_tag_match": True,
                "use_ann": True,
                "name_distance_threshold": 2.0,
            },
            format='json'
        )
        
        data_triple = response_triple.json()
        data_ann = response_ann.json()
        
        # Both should only return entities with amenity=restaurant
        if data_triple['count'] > 0:
            for result in data_triple['results']:
                self.assertEqual(result['tags'].get('amenity'), 'restaurant')
        
        if data_ann['count'] > 0:
            for result in data_ann['results']:
                self.assertEqual(result['tags'].get('amenity'), 'restaurant')
        
        print(f"\nWith exact tag match:")
        print(f"Triple-space: {data_triple['count']} results")
        print(f"ANN:          {data_ann['count']} results")

    def test_result_overlap(self):
        """Test overlap between triple-space and ANN results"""
        # Triple-space search
        response_triple = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": False,
            },
            format='json'
        )
        
        # ANN search
        response_ann = self.client.post(
            '/api/nca/semantic-triplet-search/',
            {
                "country_code": self.country_code,
                "query_tags": self.query_tags,
                "top_k": self.top_k,
                "use_ann": True,
                "name_distance_threshold": 2.0,
            },
            format='json'
        )
        
        data_triple = response_triple.json()
        data_ann = response_ann.json()
        
        # Get entity IDs
        triple_ids = {(r['osm_type'], r['osm_id']) for r in data_triple['results']}
        ann_ids = {(r['osm_type'], r['osm_id']) for r in data_ann['results']}
        
        # Calculate overlap
        overlap = triple_ids & ann_ids
        overlap_pct = len(overlap) / len(triple_ids) * 100 if triple_ids else 0
        
        print(f"\nResult overlap:")
        print(f"Triple-space: {len(triple_ids)} unique entities")
        print(f"ANN:          {len(ann_ids)} unique entities")
        print(f"Overlap:      {len(overlap)} entities ({overlap_pct:.1f}%)")

    def test_static_embedding_coverage(self):
        """Check static_embedding coverage for test country"""
        bbox = resolve_country_bbox(self.country_code, None)
        if bbox:
            from django.contrib.gis.geos import Polygon
            min_lon, min_lat, max_lon, max_lat = bbox
            polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
            
            total = OsmEntity.objects.using('vectors').filter(geom__within=polygon).count()
            with_static = OsmEntity.objects.using('vectors').filter(
                geom__within=polygon,
                static_embedding__isnull=False
            ).count()
            
            coverage = with_static / total * 100 if total > 0 else 0
            
            print(f"\nStatic embedding coverage for {self.country_code}:")
            print(f"Total entities: {total}")
            print(f"With static_embedding: {with_static}")
            print(f"Coverage: {coverage:.1f}%")
            
            # ANN requires static_embedding
            if coverage < 50:
                print(f"WARNING: Low static_embedding coverage ({coverage:.1f}%)")
                print(f"ANN search may return fewer results than triple-space")
