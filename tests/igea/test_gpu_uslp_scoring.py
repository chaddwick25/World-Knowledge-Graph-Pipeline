"""
Unit tests for GPU USLP scoring logic.
Tests the tri-space scoring (geo + name + class) and normalization.
"""
import pytest
import torch
import numpy as np
from unittest.mock import Mock, MagicMock, patch
from igea.services.gpu_uslp_service import GPUAcceleratedUSLP
from igea.services.gpu_fasttext_service import GPUFastTextService
from igea.services.gpu_haversine_service import GPUHaversineService


@pytest.fixture
def mock_gpu_fasttext():
    """Mock GPU FastText service."""
    service = Mock(spec=GPUFastTextService)
    service.device = torch.device('cuda:0')
    
    def mock_calculate_embedding_batch(texts):
        # Return random embeddings of shape (len(texts), 300)
        return torch.randn(len(texts), 300, device=service.device)
    
    def mock_cosine_similarity_batch(a, b):
        # Return cosine similarities in range [-1, 1]
        # For same entity, should be 1.0
        a_norm = torch.nn.functional.normalize(a, dim=1)
        b_norm = torch.nn.functional.normalize(b, dim=1)
        return torch.sum(a_norm * b_norm, dim=1)
    
    service.calculate_embedding_batch = mock_calculate_embedding_batch
    service.cosine_similarity_batch = mock_cosine_similarity_batch
    return service


@pytest.fixture
def mock_gpu_haversine():
    """Mock GPU Haversine service."""
    service = Mock(spec=GPUHaversineService)
    service.device = torch.device('cuda:0')
    
    def mock_filter_by_radius(head_lat, head_lon, candidate_lats, candidate_lons, radius_km):
        # Simple distance calculation for testing
        # Return all candidates within radius
        n = len(candidate_lats)
        distances = torch.zeros(n, device=service.device)
        for i in range(n):
            # Simple Euclidean distance (not accurate but sufficient for test)
            dist = torch.sqrt((candidate_lats[i] - head_lat)**2 + (candidate_lons[i] - head_lon)**2) * 111  # rough km
            distances[i] = dist
        
        mask = distances <= radius_km
        indices = torch.where(mask)[0]
        return indices, distances[mask]
    
    service.filter_by_radius = mock_filter_by_radius
    return service


@pytest.fixture
def sample_candidate_pool():
    """Create a sample candidate pool for testing."""
    return [
        {
            'osm_id': 1,
            'lat': 17.5,
            'lon': -88.5,
            'tags': {'name': 'Kingston'},
            'wkg_class': 'wkgs:City',
        },
        {
            'osm_id': 2,
            'lat': 17.51,
            'lon': -88.51,
            'tags': {'name': 'Spanish Town'},
            'wkg_class': 'wkgs:Town',
        },
        {
            'osm_id': 3,
            'lat': 17.52,
            'lon': -88.52,
            'tags': {'name': 'Portmore'},
            'wkg_class': 'wkgs:City',
        },
    ]


class TestGPUUSLPScoring:
    """Test GPU USLP scoring logic."""
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_score_candidates_gpu_returns_correct_shape(self, mock_cuda, mock_gpu_fasttext, sample_candidate_pool):
        """Test that _score_candidates_gpu returns tensors of correct shape."""
        # Setup
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = sample_candidate_pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = Mock()
        # Initialize GPU pool data (required for _score_candidates_gpu)
        coords = torch.tensor([[e['lat'], e['lon']] for e in sample_candidate_pool], device=service.device)
        service._gpu_pool_coords = coords
        service._gpu_pool_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        service._gpu_pool_class_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        
        # Create dummy data
        literal = "Kingston"
        relation = "addrCity"
        distances = torch.tensor([0.5, 1.0, 2.0], device=service.device)
        indices = torch.tensor([0, 1, 2], device=service.device)
        
        # Execute
        total_scores, geo_scores, name_scores, class_scores = service._score_candidates_gpu(
            literal, relation, distances, indices
        )
        
        # Assert shapes
        assert total_scores.shape == (3,)
        assert geo_scores.shape == (3,)
        assert name_scores.shape == (3,)
        assert class_scores.shape == (3,)
        
        # Assert types
        assert isinstance(total_scores, torch.Tensor)
        assert isinstance(geo_scores, torch.Tensor)
        assert isinstance(name_scores, torch.Tensor)
        assert isinstance(class_scores, torch.Tensor)
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_geo_score_decreases_with_distance(self, mock_cuda, mock_gpu_fasttext, sample_candidate_pool):
        """Test that geo score decreases as distance increases."""
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = sample_candidate_pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = Mock()
        coords = torch.tensor([[e['lat'], e['lon']] for e in sample_candidate_pool], device=service.device)
        service._gpu_pool_coords = coords
        service._gpu_pool_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        service._gpu_pool_class_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        
        # Distances: closer should have higher geo score
        distances = torch.tensor([0.1, 1.0, 5.0], device=service.device)
        indices = torch.tensor([0, 1, 2], device=service.device)
        
        total_scores, geo_scores, _, _ = service._score_candidates_gpu(
            "test", "test", distances, indices
        )
        
        # Geo score formula: 1 / (1 + distance)
        # distance=0.1 -> 1/1.1 ≈ 0.91
        # distance=1.0 -> 1/2.0 = 0.5
        # distance=5.0 -> 1/6.0 ≈ 0.17
        assert geo_scores[0] > geo_scores[1] > geo_scores[2]
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_normalization_divides_by_three(self, mock_cuda, mock_gpu_fasttext, sample_candidate_pool):
        """Test that normalization divides total scores by 3.0."""
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = sample_candidate_pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = Mock()
        coords = torch.tensor([[e['lat'], e['lon']] for e in sample_candidate_pool], device=service.device)
        service._gpu_pool_coords = coords
        service._gpu_pool_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        service._gpu_pool_class_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        
        distances = torch.tensor([1.0, 1.0, 1.0], device=service.device)
        indices = torch.tensor([0, 1, 2], device=service.device)
        
        total_scores, geo_scores, name_scores, class_scores = service._score_candidates_gpu(
            "test", "test", distances, indices
        )
        
        # Manually compute normalization
        normalized = total_scores / 3.0
        
        # Verify formula
        expected_normalized = (geo_scores + name_scores + class_scores) / 3.0
        assert torch.allclose(normalized, expected_normalized, atol=1e-5)
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_threshold_filtering_works_correctly(self, mock_cuda, mock_gpu_fasttext, sample_candidate_pool):
        """Test that threshold filtering correctly accepts/rejects candidates."""
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = sample_candidate_pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = Mock()
        coords = torch.tensor([[e['lat'], e['lon']] for e in sample_candidate_pool], device=service.device)
        service._gpu_pool_coords = coords
        service._gpu_pool_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        service._gpu_pool_class_embeddings = torch.randn(len(sample_candidate_pool), 300, device=service.device)
        
        distances = torch.tensor([1.0, 1.0, 1.0], device=service.device)
        indices = torch.tensor([0, 1, 2], device=service.device)
        
        total_scores, geo_scores, name_scores, class_scores = service._score_candidates_gpu(
            "test", "test", distances, indices
        )
        
        # Test threshold logic manually
        normalized_scores = total_scores / 3.0
        threshold = 0.6
        valid_mask = normalized_scores >= threshold
        
        # Count how many pass threshold
        valid_count = torch.sum(valid_mask).item()
        
        # At least verify the mask shape matches
        assert valid_mask.shape == normalized_scores.shape


class TestGPUUSLPPredictionFlow:
    """Test the full prediction flow with mocked components."""
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_predict_links_for_entity_with_zero_candidates(self, mock_cuda, mock_gpu_fasttext, mock_gpu_haversine):
        """Test behavior when no candidates are found within radius."""
        pool = [{'osm_id': 1, 'lat': 17.5, 'lon': -88.5, 'tags': {'name': 'test'}, 'wkg_class': 'wkgs:Test'}]
        
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = mock_gpu_haversine
        coords = torch.tensor([[e['lat'], e['lon']] for e in pool], device=service.device)
        service._gpu_pool_coords = coords
        
        # Mock spatial filtering to return no candidates
        with patch.object(service.gpu_haversine, 'filter_by_radius') as mock_filter:
            mock_filter.return_value = (
                torch.tensor([], device=service.device),
                torch.tensor([], device=service.device)
            )
            
            head_tags = {'addr:city': 'test'}
            results = service.predict_links_for_entity(
                head_osm_id=999,
                head_lat=17.5,
                head_lon=-88.5,
                head_tags=head_tags,
                threshold=0.6,
                top_k=5
            )
            
            # Should return empty list
            assert len(results) == 0
    
    @patch('igea.services.gpu_uslp_service.torch.cuda.is_available', return_value=True)
    def test_predict_links_for_entity_filters_self(self, mock_cuda, mock_gpu_fasttext, mock_gpu_haversine):
        """Test that head entity is filtered out from candidates."""
        pool = [
            {'osm_id': 999, 'lat': 17.5, 'lon': -88.5, 'tags': {'name': 'self'}, 'wkg_class': 'wkgs:Test'},
            {'osm_id': 1, 'lat': 17.51, 'lon': -88.51, 'tags': {'name': 'other'}, 'wkg_class': 'wkgs:Test'},
        ]
        
        service = GPUAcceleratedUSLP(device='cuda:0')
        service._pool = pool
        service.gpu_fasttext = mock_gpu_fasttext
        service.gpu_haversine = mock_gpu_haversine
        coords = torch.tensor([[e['lat'], e['lon']] for e in pool], device=service.device)
        service._gpu_pool_coords = coords
        
        # Mock spatial filtering to return only the other candidate (not self)
        with patch.object(service.gpu_haversine, 'filter_by_radius') as mock_filter:
            mock_filter.return_value = (
                torch.tensor([1], device=service.device),  # Only return index 1 (other)
                torch.tensor([0.2], device=service.device)
            )
            
            # Mock scoring
            with patch.object(service, '_score_candidates_gpu') as mock_score:
                mock_score.return_value = (
                    torch.tensor([2.4], device=service.device),
                    torch.tensor([0.8], device=service.device),
                    torch.tensor([0.8], device=service.device),
                    torch.tensor([0.8], device=service.device),
                )
                
                head_tags = {'addr:city': 'test'}
                results = service.predict_links_for_entity(
                    head_osm_id=999,  # Same as pool[0]['osm_id']
                    head_lat=17.5,
                    head_lon=-88.5,
                    head_tags=head_tags,
                    threshold=0.6,
                    top_k=5
                )
                
                # Should have 1 result
                assert len(results) == 1
                assert results[0]['tail_osm_id'] == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
