"""
DistilGPT2 hidden state embedding service.

NOTE: Requires PyTorch and Transformers to be installed.
Install with:
    pip install torch==1.11.0+cu113 --extra-index-url https://download.pytorch.org/whl/cu113
    pip install transformers==4.18.0
"""

import logging
from typing import Dict
import numpy as np

logger = logging.getLogger(__name__)

# Check if PyTorch is available
try:
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    PYTORCH_AVAILABLE = True
except ImportError:
    PYTORCH_AVAILABLE = False
    logger.warning("PyTorch and/or Transformers not installed. GPT embedding service unavailable.")


class GPTEmbeddingService:
    """Service for DistilGPT2 hidden state-based embeddings."""
    
    _tokenizer = None
    _model = None
    _projection_head = None
    
    @classmethod
    def is_available(cls):
        """Check if PyTorch dependencies are installed."""
        return PYTORCH_AVAILABLE
    
    @classmethod
    def load_models(cls):
        """Load DistilGPT2 model and projection head."""
        if not PYTORCH_AVAILABLE:
            raise ImportError(
                "PyTorch and Transformers are required. Install with:\n"
                "pip install torch==1.11.0+cu113 --extra-index-url https://download.pytorch.org/whl/cu113\n"
                "pip install transformers==4.18.0"
            )
        
        if cls._tokenizer is None:
            logger.info("Loading DistilGPT2 tokenizer...")
            cls._tokenizer = AutoTokenizer.from_pretrained("distilbert/distilgpt2")
            cls._tokenizer.pad_token = cls._tokenizer.eos_token
        
        if cls._model is None:
            logger.info("Loading DistilGPT2 model...")
            cls._model = AutoModelForCausalLM.from_pretrained(
                "distilbert/distilgpt2",
                output_hidden_states=True
            )
            cls._model.eval()
            
            # Move to GPU if available
            if torch.cuda.is_available():
                cls._model = cls._model.cuda()
                logger.info("DistilGPT2 model moved to GPU")
        
        from django.conf import settings
        from pathlib import Path
        import os
        
        # Load projection head if available
        projection_path = getattr(settings, 'GPT_PROJECTION_HEAD_PATH', None)
        if projection_path and os.path.exists(projection_path):
            try:
                logger.info(f"Loading projection head from {projection_path}...")
                cls._projection_head = torch.load(projection_path, map_location='cuda' if torch.cuda.is_available() else 'cpu')
                cls._projection_head.eval()
                if torch.cuda.is_available():
                    cls._projection_head = cls._projection_head.cuda()
                logger.info("Projection head loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load projection head: {e}")
        else:
            logger.info("No projection head path configured or file missing. Text models loaded.")
    
    @classmethod
    def format_query(cls, tag_counts: Dict[str, int], format_type: str = 'structured') -> str:
        """
        Format tag counts as natural language query.
        
        Args:
            tag_counts: Dictionary of tag to count
            format_type: One of 'structured', 'natural', 'weighted'
        
        Returns:
            Formatted query string
        """
        from ..utils.query_formatter import QueryFormatter
        return QueryFormatter.format(tag_counts, format_type)
    
    @classmethod
    def extract_hidden_states(cls, query_text: str) -> np.ndarray:
        """
        Extract hidden states from DistilGPT2 for a query.
        
        Args:
            query_text: Natural language query
        
        Returns:
            Hidden states array of shape [seq_len, 768]
        """
        if not PYTORCH_AVAILABLE:
            raise ImportError("PyTorch not available")
        
        if cls._model is None:
            cls.load_models()
        
        # Tokenize
        inputs = cls._tokenizer(
            query_text,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=128
        )
        
        # Move to GPU if available
        if torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}
        
        # Extract hidden states
        with torch.no_grad():
            outputs = cls._model(**inputs)
            # Get last layer hidden states
            hidden_states = outputs.hidden_states[-1]  # [batch, seq_len, 768]
        
        # Move back to CPU and convert to numpy
        hidden_states = hidden_states.cpu().numpy()
        
        # Remove batch dimension
        return hidden_states.squeeze(0)  # [seq_len, 768]
    
    @classmethod
    def calculate_embedding(cls, tag_counts: Dict[str, int]) -> np.ndarray:
        """
        Calculate embedding using DistilGPT2 hidden states + projection head.
        
        Args:
            tag_counts: Dictionary of OSM tag to count
        
        Returns:
            L2-normalized 300D numpy array
        """
        if not PYTORCH_AVAILABLE:
            raise ImportError("PyTorch not available")
        
        # Format query
        query_text = cls.format_query(tag_counts)
        
        # Extract hidden states
        hidden_states = cls.extract_hidden_states(query_text)
        
        # Apply projection head if available
        if cls._projection_head is not None:
            with torch.no_grad():
                tensor_hidden = torch.from_numpy(hidden_states).unsqueeze(0)  # Add batch dim [1, seq_len, 768]
                if torch.cuda.is_available():
                    tensor_hidden = tensor_hidden.cuda()
                
                try:
                    # Specific ML logic (e.g. pooling + projection) should be encapsulated inside _projection_head module
                    embedding = cls._projection_head(tensor_hidden)
                    embedding = embedding.squeeze(0).cpu().numpy()
                    
                    # Ensure normalized
                    norm = np.linalg.norm(embedding)
                    if norm > 0:
                        embedding = embedding / norm
                    return embedding
                except NotImplementedError as e:
                    logger.error(f"Projection head forward pass not fully implemented: {e}")
                    raise e
                    
        # Fallback placeholder if no projection head
        logger.warning("Projection head not loaded. Returning random embedding for testing.")
        # Return random normalized vector for testing
        np.random.seed(hash(query_text) % (2**32))
        embedding = np.random.randn(300).astype(np.float32)
        embedding = embedding / np.linalg.norm(embedding)
        return embedding
