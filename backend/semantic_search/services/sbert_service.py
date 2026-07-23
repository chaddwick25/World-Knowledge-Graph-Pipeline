"""
SBERT Embedding Service for GPU-accelerated semantic encoding.

Provides sentence-transformer based embeddings on GPU with projection head
to match the existing 300D FastText space.
"""

import logging
import numpy as np
from typing import Optional, Dict
from django.conf import settings
import os

logger = logging.getLogger(__name__)

_torch = None
_nn = None
_torch_import_error = None


def _ensure_torch():
    global _torch, _nn, _torch_import_error
    if _torch is not None or _torch_import_error is not None:
        return _torch
    try:
        import torch as torch_mod
        import torch.nn as nn_mod
        _torch = torch_mod
        _nn = nn_mod
        return _torch
    except Exception as e:
        _torch_import_error = e
        logger.warning(f"SBERT: torch unavailable or misconfigured; SBERT will be disabled: {e}")
        _torch = None
        return None


class SBERTEmbeddingService:
    """
    GPU-accelerated SBERT embedding service with projection head.

    Uses sentence-transformers for semantic encoding and a learnable
    projection head to match the 300D FastText embedding space.

    Supports region-specific projection heads (country/subgraph level)
    following the GV-NLE pattern.
    """

    _model = None
    _projection_heads = {}  # region -> projection_head
    _device = None
    
    @classmethod
    def get_model(cls):
        """Lazy-load SBERT model on GPU."""
        if cls._model is not None:
            return cls._model

        torch_mod = _ensure_torch()
        if torch_mod is None:
            cls._device = None
            cls._model = None
            return cls._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning("sentence-transformers not installed. SBERT methods will return zero vectors.")
            cls._device = None
            cls._model = None
            return cls._model
        except Exception as e:
            logger.error(f"Failed to import sentence-transformers: {e}", exc_info=True)
            cls._device = None
            cls._model = None
            return cls._model

        # Use a small, efficient model
        model_name = 'all-MiniLM-L6-v2'  # 80MB, 384D output
        try:
            cls._device = torch_mod.device('cuda:0' if torch_mod.cuda.is_available() else 'cpu')
            logger.info(f"Loading SBERT model {model_name} on {cls._device}...")
            cls._model = SentenceTransformer(model_name, device=cls._device)
            logger.info(f"SBERT model loaded on {cls._device}")
        except Exception as e:
            logger.error(f"Failed to load SBERT: {e}", exc_info=True)
            cls._device = None
            cls._model = None

        return cls._model
    
    @classmethod
    def get_projection_head(cls, region: str = None, base_path: str = None):
        """Lazy-load projection head (384D → 300D) for a specific region.

        Args:
            region: Region slug (country or subgraph). If None, uses global fallback.
            base_path: Base path for the country (e.g., /media/.../belize).
                      If None, falls back to settings.BASE_DATA_DIR.

        Returns:
            Projection head model or None if unavailable.
        """
        # Use global fallback if no region specified
        if region is None:
            region = "global"

        # Return cached projection head if available
        cache_key = f"{region}:{base_path}" if base_path else region
        if cache_key in cls._projection_heads:
            return cls._projection_heads[cache_key]

        if cls._device is None:
            model = cls.get_model()
            if model is None or cls._device is None:
                return None

        torch_mod = _ensure_torch()
        if torch_mod is None:
            return None

        try:
            if _nn is None:
                _ensure_torch()

            # Initialize projection head
            projection = _nn.Linear(384, 300).to(cls._device)

            # Try to load region-specific trained weights from subgraph directory structure
            if base_path:
                # New structure: {base_path}/projection_head/{region}/projection_head.pt
                base_path = Path(base_path)
                region_path = base_path / "projection_head" / region / "projection_head.pt"
                if region_path.exists():
                    logger.info(f"Loading region-specific projection head for '{region}' from {region_path}")
                    projection.load_state_dict(
                        torch_mod.load(region_path, map_location=cls._device)
                    )
                    projection.eval()
                    cls._projection_heads[cache_key] = projection
                    return projection
                else:
                    logger.warning(f"Region-specific projection head not found at {region_path}")

            # Fallback to old structure for backwards compatibility
            base_data = settings.BASE_DATA_DIR or str(settings.BASE_DIR / "data")
            models_dir = Path(base_data) / "models" / "sbert_projections"
            old_region_path = models_dir / f"{region}_projection_head.pt"

            if old_region_path.exists():
                logger.info(f"Using old-style projection head for region '{region}' from {old_region_path}")
                projection.load_state_dict(
                    torch_mod.load(old_region_path, map_location=cls._device)
                )
                projection.eval()
                cls._projection_heads[cache_key] = projection
                return projection

            # Fallback to global projection head
            global_path = Path(base_data) / "models" / "sbert_projection_head.pt"
            if global_path.exists():
                logger.info(f"Using global projection head for region '{region}' from {global_path}")
                projection.load_state_dict(
                    torch_mod.load(global_path, map_location=cls._device)
                )
                projection.eval()
                cls._projection_heads[cache_key] = projection
                return projection

            # No trained projection head found, use random initialization
            logger.warning(f"No trained projection head found for region '{region}', using random initialization")
            _nn.init.xavier_uniform_(projection.weight)
            cls._projection_heads[cache_key] = projection
            projection.eval()
            return projection

        except Exception as e:
            logger.error(f"Failed to load projection head for region '{region}': {e}", exc_info=True)
            cls._projection_heads[cache_key] = None
            return None
    
    @classmethod
    def encode_text(cls, text: str, region: str = None, base_path: str = None) -> np.ndarray:
        """
        Encode text using SBERT and project to 300D space.

        Args:
            text: Input text string
            region: Region slug (country/subgraph) for region-specific projection head
            base_path: Base path for the country (e.g., /media/.../belize)

        Returns:
            300-dimensional L2-normalized embedding vector
        """
        model = cls.get_model()
        projection = cls.get_projection_head(region=region, base_path=base_path)

        if not text:
            return np.zeros(300, dtype=np.float32)
        if model is None or projection is None or cls._device is None:
            return np.zeros(300, dtype=np.float32)

        torch_mod = _ensure_torch()
        if torch_mod is None:
            return np.zeros(300, dtype=np.float32)

        try:
            # Encode with SBERT (returns 384D)
            with torch_mod.no_grad():
                embedding_384 = model.encode(text, convert_to_tensor=True, show_progress_bar=False)

                # Move to correct device if needed
                if isinstance(embedding_384, np.ndarray):
                    embedding_384 = torch_mod.from_numpy(embedding_384).to(cls._device)
                elif hasattr(embedding_384, "device") and embedding_384.device != cls._device:
                    embedding_384 = embedding_384.to(cls._device)

                # Project to 300D
                embedding_300 = projection(embedding_384)

                # L2 normalization
                embedding_300 = torch_mod.nn.functional.normalize(embedding_300, p=2, dim=-1)

                # Convert to numpy
                result = embedding_300.cpu().numpy().astype(np.float32)

                return result

        except Exception as e:
            logger.error(f"SBERT encoding error: {e}", exc_info=True)
            return np.zeros(300, dtype=np.float32)
    
    @classmethod
    def encode_tags(cls, tag_counts: Dict[str, int], region: str = None, base_path: str = None) -> np.ndarray:
        """
        Encode tag counts using SBERT and project to 300D space.

        Converts tag counts to a text representation and encodes it.

        Args:
            tag_counts: Dictionary of tag to count
            region: Region slug (country/subgraph) for region-specific projection head
            base_path: Base path for the country (e.g., /media/.../belize)

        Returns:
            300-dimensional L2-normalized embedding vector
        """
        if not tag_counts:
            return np.zeros(300, dtype=np.float32)

        # Convert tag counts to text representation
        # Repeat tags according to their counts
        text_parts = []
        for tag, count in tag_counts.items():
            text_parts.extend([tag] * count)

        text = " ".join(text_parts)
        return cls.encode_text(text, region=region, base_path=base_path)
    
    @classmethod
    def batch_encode(cls, texts: list, region: str = None, base_path: str = None) -> np.ndarray:
        """
        Encode multiple texts in batch.

        Args:
            texts: List of text strings
            region: Region slug (country/subgraph) for region-specific projection head
            base_path: Base path for the country (e.g., /media/.../belize)

        Returns:
            Array of shape (len(texts), 300)
        """
        model = cls.get_model()
        projection = cls.get_projection_head(region=region, base_path=base_path)

        if not texts:
            return np.zeros((0, 300), dtype=np.float32)
        if model is None or projection is None or cls._device is None:
            return np.zeros((len(texts), 300), dtype=np.float32)

        torch_mod = _ensure_torch()
        if torch_mod is None:
            return np.zeros((len(texts), 300), dtype=np.float32)

        try:
            with torch_mod.no_grad():
                # Batch encode with SBERT
                embeddings_384 = model.encode(texts, convert_to_tensor=True, show_progress_bar=False)

                # Move to correct device if needed
                if isinstance(embeddings_384, np.ndarray):
                    embeddings_384 = torch_mod.from_numpy(embeddings_384).to(cls._device)
                elif hasattr(embeddings_384, "device") and embeddings_384.device != cls._device:
                    embeddings_384 = embeddings_384.to(cls._device)

                # Project to 300D
                embeddings_300 = projection(embeddings_384)

                # L2 normalization
                embeddings_300 = torch_mod.nn.functional.normalize(embeddings_300, p=2, dim=-1)

                # Convert to numpy
                result = embeddings_300.cpu().numpy().astype(np.float32)

                return result

        except Exception as e:
            logger.error(f"SBERT batch encoding error: {e}", exc_info=True)
            return np.zeros((len(texts), 300), dtype=np.float32)
    
    @classmethod
    def _create_mock_sbert(cls):
        """Provides a lightweight mock for environments without sentence-transformers."""
        class MockSBERT:
            def encode(self, text, convert_to_tensor=False, show_progress_bar=False):
                # Deterministic random vector for stable testing
                np.random.seed(hash(text) % (2**32))
                vec = np.random.randn(384).astype(np.float32)
                if convert_to_tensor:
                    torch_mod = _ensure_torch()
                    if torch_mod is not None:
                        return torch_mod.from_numpy(vec)
                return vec
        return MockSBERT()
    
    @classmethod
    def get_device_info(cls) -> Dict:
        """Get device information."""
        if cls._device is None:
            cls.get_model()
        
        torch_mod = _ensure_torch()
        cuda_available = False
        if torch_mod is not None:
            try:
                cuda_available = torch_mod.cuda.is_available()
            except Exception:
                cuda_available = False
        
        return {
            'device': str(cls._device) if cls._device is not None else 'cpu',
            'cuda_available': cuda_available,
            'model_loaded': cls._model is not None,
            'projection_loaded': cls._projection_head is not None
        }
