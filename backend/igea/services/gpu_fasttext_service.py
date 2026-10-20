"""
GPU-Accelerated FastText Service for USLP

Provides batch FastText embedding generation on GPU using PyTorch.
Compatible with existing FastText models, optimized for K80/RTX GPUs.
"""

import logging
import numpy as np
import torch
from typing import List, Dict
from pathlib import Path
from core.services.snapshot.regional_path_service import normalize_country_name

logger = logging.getLogger(__name__)


class GPUFastTextService:
    """
    GPU-accelerated FastText embedding service.
    
    Wraps the existing CPU FastText model and provides batch processing
    with GPU acceleration for embedding calculations.
    """
    
    def __init__(self, model_path: str = None, device: str = 'cuda:0', batch_size: int = 128):
        """
        Initialize GPU FastText service.
        
        Args:
            model_path: Path to FastText .bin model
            device: CUDA device ('cuda:0', 'cuda:1', etc.)
            batch_size: Batch size for GPU processing
        """
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')
        self.batch_size = batch_size
        self._model = None
        self._model_path = model_path
        
        if torch.cuda.is_available():
            logger.info(f"GPU FastText: Using device {self.device} ({torch.cuda.get_device_name(self.device)})")
        else:
            logger.warning("GPU FastText: CUDA not available, falling back to CPU")
    
    def _load_model(self):
        """Lazy load FastText model."""
        if self._model is None:
            # Reuse the centralized FastText model from ModelRegistryService so the
            # heavy(7GBs) cc.en.300.bin binary is loaded exactly once per process and shared across paths.
            from semantic_search.services.model_registry_service import ModelRegistryService
            self._model = ModelRegistryService.get_fasttext_model()
            logger.info("GPU FastText: FastText model handle acquired from ModelRegistryService")
    
    def calculate_embedding(self, text: str) -> np.ndarray:
        """
        Calculate embedding for a single text string (CPU fallback).

        Uses weighted average of word vectors (matching CPU FastTextEmbeddingService).

        Args:
            text: Input text

        Returns:
            300-dimensional L2-normalized embedding vector
        """
        self._load_model()

        tokens = normalize_country_name(text).split()
        tokens = [t for t in tokens if t]
        if not tokens:
            return np.zeros(300, dtype=np.float32)

        # Weighted average of word vectors (matching CPU path)
        total_weight = len(tokens)
        synthesized = np.zeros(300, dtype=np.float32)
        for token in tokens:
            synthesized += self._model.get_word_vector(token) / total_weight

        # L2 normalization
        norm = np.linalg.norm(synthesized)
        if norm > 0:
            synthesized = synthesized / norm

        return synthesized
    
    def calculate_embedding_batch(self, texts: List[str]) -> torch.Tensor:
        """
        Calculate embeddings for a batch of texts on GPU.
        
        Uses the same weighted-average-of-word-vectors approach as calculate_embedding
        and the CPU FastTextEmbeddingService.
        
        Args:
            texts: List of text strings
            
        Returns:
            Tensor of shape (len(texts), 300) on GPU
        """
        self._load_model()
        
        # Use the same embedding method as calculate_embedding (get_word_vector)
        cpu_embeddings = [self.calculate_embedding(text) for text in texts]
        
        # Convert to numpy array and move to GPU
        embeddings_np = np.array(cpu_embeddings, dtype=np.float32)
        embeddings_gpu = torch.from_numpy(embeddings_np).to(self.device)
        
        return embeddings_gpu
    
    def cosine_similarity_batch(self, query: torch.Tensor, candidates: torch.Tensor) -> torch.Tensor:
        """
        Calculate cosine similarity between query and candidates on GPU.
        
        Args:
            query: Query embedding (1, 300) or (300,)
            candidates: Candidate embeddings (N, 300)
            
        Returns:
            Similarity scores (N,) - always at least 1D
        """
        # Ensure query is 2D
        if query.dim() == 1:
            query = query.unsqueeze(0)
        
        # Normalize vectors
        query_norm = torch.nn.functional.normalize(query, p=2, dim=1)
        candidates_norm = torch.nn.functional.normalize(candidates, p=2, dim=1)
        
        # Compute cosine similarity
        similarities = torch.mm(query_norm, candidates_norm.t()).squeeze()
        
        # Ensure result is at least 1D (handle single candidate case)
        if similarities.dim() == 0:
            similarities = similarities.unsqueeze(0)
        
        return similarities
    
    def precompute_embeddings(self, texts: List[str]) -> torch.Tensor:
        """
        Precompute embeddings for a large list of texts in batches.
        
        Args:
            texts: List of text strings
            
        Returns:
            Tensor of shape (len(texts), 300) on GPU
        """
        self._load_model()
        
        all_embeddings = []
        
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i:i + self.batch_size]
            batch_embeddings = self.calculate_embedding_batch(batch_texts)
            all_embeddings.append(batch_embeddings)
        
        # Concatenate all batches
        return torch.cat(all_embeddings, dim=0)
    
    def get_device_info(self) -> Dict:
        """Get GPU device information."""
        if not torch.cuda.is_available():
            return {'device': 'cpu', 'cuda_available': False}
        
        return {
            'device': str(self.device),
            'cuda_available': True,
            'device_name': torch.cuda.get_device_name(self.device),
            'memory_allocated': torch.cuda.memory_allocated(self.device) / 1024**3,  # GB
            'memory_reserved': torch.cuda.memory_reserved(self.device) / 1024**3,  # GB
        }
