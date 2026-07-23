"""
GPU-Accelerated Haversine Distance Service

Provides vectorized haversine distance calculations on GPU using PyTorch.
Optimized for K80 FP64 precision and RTX tensor cores.
"""

import logging
import torch
import numpy as np
from typing import Tuple, Union

logger = logging.getLogger(__name__)


class GPUHaversineService:
    """
    GPU-accelerated haversine distance calculations.
    
    Provides vectorized distance computations between geographic coordinates
    using PyTorch for GPU acceleration.
    """
    
    EARTH_RADIUS_KM = 6371.0
    
    def __init__(self, device: str = 'cuda:0', use_fp64: bool = False):
        """
        Initialize GPU Haversine service.
        
        Args:
            device: CUDA device ('cuda:0', 'cuda:1', etc.)
            use_fp64: Use FP64 precision (recommended for K80)
        """
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')
        self.dtype = torch.float64 if use_fp64 else torch.float32
        
        if torch.cuda.is_available():
            logger.info(f"GPU Haversine: Using device {self.device} ({torch.cuda.get_device_name(self.device)})")
            logger.info(f"GPU Haversine: Precision = {'FP64' if use_fp64 else 'FP32'}")
        else:
            logger.warning("GPU Haversine: CUDA not available, falling back to CPU")
    
    def haversine_single(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate haversine distance between two points (CPU fallback).
        
        Args:
            lat1, lon1: First point coordinates (degrees)
            lat2, lon2: Second point coordinates (degrees)
            
        Returns:
            Distance in kilometers
        """
        # Convert to radians
        lat1_rad = np.radians(lat1)
        lon1_rad = np.radians(lon1)
        lat2_rad = np.radians(lat2)
        lon2_rad = np.radians(lon2)
        
        # Haversine formula
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad
        
        a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
        c = 2 * np.arcsin(np.sqrt(a))
        
        return self.EARTH_RADIUS_KM * c
    
    def haversine_batch(self, 
                       lat1: Union[torch.Tensor, np.ndarray, float],
                       lon1: Union[torch.Tensor, np.ndarray, float],
                       lat2: Union[torch.Tensor, np.ndarray, float],
                       lon2: Union[torch.Tensor, np.ndarray, float]) -> torch.Tensor:
        """
        Calculate haversine distances in batch on GPU.
        
        Args:
            lat1, lon1: Head coordinates (scalar or tensor)
            lat2, lon2: Candidate coordinates (N,) or (M, N)
            
        Returns:
            Distance tensor in kilometers
        """
        # Convert inputs to tensors
        lat1_t = self._to_tensor(lat1)
        lon1_t = self._to_tensor(lon1)
        lat2_t = self._to_tensor(lat2)
        lon2_t = self._to_tensor(lon2)
        
        # Convert to radians
        lat1_rad = torch.deg2rad(lat1_t)
        lon1_rad = torch.deg2rad(lon1_t)
        lat2_rad = torch.deg2rad(lat2_t)
        lon2_rad = torch.deg2rad(lon2_t)
        
        # Haversine formula (vectorized)
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad
        
        a = torch.sin(dlat/2)**2 + torch.cos(lat1_rad) * torch.cos(lat2_rad) * torch.sin(dlon/2)**2
        c = 2 * torch.arcsin(torch.sqrt(a))
        
        return self.EARTH_RADIUS_KM * c
    
    def haversine_matrix(self,
                        lat1: Union[torch.Tensor, np.ndarray],
                        lon1: Union[torch.Tensor, np.ndarray],
                        lat2: Union[torch.Tensor, np.ndarray],
                        lon2: Union[torch.Tensor, np.ndarray]) -> torch.Tensor:
        """
        Calculate pairwise haversine distance matrix on GPU.
        
        Args:
            lat1, lon1: Head coordinates (N,)
            lat2, lon2: Candidate coordinates (M,)
            
        Returns:
            Distance matrix (N, M) in kilometers
        """
        # Convert to tensors
        lat1_t = self._to_tensor(lat1)
        lon1_t = self._to_tensor(lon1)
        lat2_t = self._to_tensor(lat2)
        lon2_t = self._to_tensor(lon2)
        
        # Ensure 1D
        lat1_t = lat1_t.flatten()
        lon1_t = lon1_t.flatten()
        lat2_t = lat2_t.flatten()
        lon2_t = lon2_t.flatten()
        
        # Convert to radians
        lat1_rad = torch.deg2rad(lat1_t)
        lon1_rad = torch.deg2rad(lon1_t)
        lat2_rad = torch.deg2rad(lat2_t)
        lon2_rad = torch.deg2rad(lon2_t)
        
        # Broadcast to (N, M) shape
        lat1_rad = lat1_rad.unsqueeze(1)  # (N, 1)
        lon1_rad = lon1_rad.unsqueeze(1)  # (N, 1)
        lat2_rad = lat2_rad.unsqueeze(0)  # (1, M)
        lon2_rad = lon2_rad.unsqueeze(0)  # (1, M)
        
        # Haversine formula (broadcasted)
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad
        
        a = torch.sin(dlat/2)**2 + torch.cos(lat1_rad) * torch.cos(lat2_rad) * torch.sin(dlon/2)**2
        c = 2 * torch.arcsin(torch.sqrt(a))
        
        return self.EARTH_RADIUS_KM * c
    
    def filter_by_radius(self,
                        head_lat: float,
                        head_lon: float,
                        candidate_lats: Union[torch.Tensor, np.ndarray],
                        candidate_lons: Union[torch.Tensor, np.ndarray],
                        radius_km: float) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Filter candidates within radius of head entity on GPU.
        
        Args:
            head_lat, head_lon: Head entity coordinates
            candidate_lats, candidate_lons: Candidate coordinates (N,)
            radius_km: Search radius in kilometers
            
        Returns:
            Tuple of (indices, distances) for candidates within radius
        """
        # Calculate distances
        distances = self.haversine_batch(head_lat, head_lon, candidate_lats, candidate_lons)
        
        # Filter by radius
        mask = distances <= radius_km
        indices = torch.where(mask)[0]
        filtered_distances = distances[mask]
        
        return indices, filtered_distances
    
    def _to_tensor(self, data: Union[torch.Tensor, np.ndarray, float]) -> torch.Tensor:
        """Convert input to GPU tensor."""
        if isinstance(data, torch.Tensor):
            return data.to(device=self.device, dtype=self.dtype)
        elif isinstance(data, np.ndarray):
            return torch.from_numpy(data).to(device=self.device, dtype=self.dtype)
        else:
            return torch.tensor(data, device=self.device, dtype=self.dtype)
    
    def get_device_info(self) -> dict:
        """Get GPU device information."""
        if not torch.cuda.is_available():
            return {'device': 'cpu', 'cuda_available': False}
        
        return {
            'device': str(self.device),
            'cuda_available': True,
            'device_name': torch.cuda.get_device_name(self.device),
            'dtype': str(self.dtype),
            'memory_allocated': torch.cuda.memory_allocated(self.device) / 1024**3,  # GB
            'memory_reserved': torch.cuda.memory_reserved(self.device) / 1024**3,  # GB
        }
