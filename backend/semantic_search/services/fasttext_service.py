import numpy as np
import fasttext
from django.conf import settings
from typing import Dict
import logging
import os
import re

from .model_registry_service import ModelRegistryService

logger = logging.getLogger(__name__)


class FastTextEmbeddingService:
    """Service for FastText GeoVector operations (baseline method)."""

    @classmethod
    def build_tag_counts_from_osm_tags(cls, tags: Dict[str, str]) -> Dict[str, int]:
        """Convert an OSM tag dict into tag_counts using key + value tokens.

        This mirrors the GeoVectors FastTextModel.encode_tags behavior:
        - Each key contributes a token
        - Each single-token value contributes a token

        Args:
            tags: OSM tag dict, e.g. {"amenity": "cafe", "name": "Starbucks"}

        Returns:
            A tag_counts dict suitable for calculate_embedding(), where
            keys are tokens (e.g. "amenity", "cafe") and values are counts.
        """
        tag_counts: Dict[str, int] = {}
        for k, v in (tags or {}).items():
            # Key token
            tag_counts[k] = tag_counts.get(k, 0) + 1

            # Single-token value token
            if isinstance(v, str):
                v = v.strip()
                if v and len(v.split()) == 1:
                    tag_counts[v] = tag_counts.get(v, 0) + 1

        return tag_counts

    @classmethod
    def calculate_embedding(cls, tag_counts: Dict[str, int]) -> np.ndarray:
        """
        Calculate weighted average GeoVector for tag counts.

        Args:
            tag_counts: Dictionary of OSM tag to count
                e.g., {'cafe': 40, 'residential': 30, 'park': 20}

        Returns:
            L2-normalized 300D numpy array
        """
        model = ModelRegistryService.get_fasttext_model()

        if not tag_counts:
            return np.zeros(300, dtype=np.float32)

        total_weight = sum(tag_counts.values())
        if total_weight == 0:
            return np.zeros(300, dtype=np.float32)

        synthesized_vector = np.zeros(300, dtype=np.float32)

        for tag, count in tag_counts.items():
            vec = model.get_word_vector(tag)
            weight = count / total_weight
            synthesized_vector += (weight * vec)

        # L2 Normalization
        norm = np.linalg.norm(synthesized_vector)
        if norm > 0:
            synthesized_vector = synthesized_vector / norm

        return synthesized_vector
    # TODO: Remember to replace this with the Concepts to Functional Role implementation
    # Common English stop words that add noise to FastText bag-of-words
    # embeddings of natural-language queries.  These tokens have FastText
    # vectors but carry no OSM-semantic signal (e.g. "find cafes in belize"
    # should embed "cafes", not "find" + "in" + "belize").
    _STOP_WORDS = frozenset({
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "find", "show", "get", "list", "search", "me", "all", "some", "many",
        "near", "around", "area", "areas", "place", "places", "where",
        "what", "which", "that", "this", "there", "here", "it", "they",
        "i", "you", "we", "he", "she", "my", "your", "our",
    })

    @classmethod
    def calculate_text_embedding(cls, text: str) -> np.ndarray:
        """Calculate embedding for a natural language query using FastText.

        This treats the query as a bag-of-words and averages FastText word
        vectors, then L2-normalizes the result to stay compatible with the
        300D GeoVectors space.  Stop words are filtered to avoid diluting
        the signal with tokens that carry no OSM-semantic meaning.

        Args:
            text: Natural language query string.

        Returns:
            L2-normalized 300D numpy array.
        """
        model = ModelRegistryService.get_fasttext_model()

        if not text:
            return np.zeros(300, dtype=np.float32)

        # Simple word-character tokenization, then stop-word removal
        tokens = [
            t for t in re.findall(r"\w+", text.lower())
            if t and t not in cls._STOP_WORDS
        ]
        if not tokens:
            return np.zeros(300, dtype=np.float32)

        vec = np.zeros(300, dtype=np.float32)
        for token in tokens:
            vec += model.get_word_vector(token)

        # Average over tokens
        vec /= float(len(tokens))

        # L2 Normalization
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        return vec

    @classmethod
    def batch_calculate_embeddings(cls, tag_counts_list: list) -> np.ndarray:
        """
        Calculate embeddings for multiple tag count dictionaries.

        Args:
            tag_counts_list: List of tag count dictionaries

        Returns:
            Array of shape (batch_size, 300)
        """
        embeddings = []
        for tag_counts in tag_counts_list:
            emb = cls.calculate_embedding(tag_counts)
            embeddings.append(emb)
        return np.array(embeddings)
