import os
import logging
import fasttext
import numpy as np
from django.conf import settings
from typing import Optional

logger = logging.getLogger(__name__)

class ModelRegistryService:
    """
    Centralized registry for heavy machine learning models (FastText, NLE, NCA).
    Ensures models are loaded exactly once into memory and shared across services.
    """
    _fasttext_model = None
    _nca_model = None
    
    @classmethod
    def get_fasttext_model(cls):
        """Lazy-loads the 300D FastText model (cc.en.300.bin)."""
        if cls._fasttext_model is None:
            config = getattr(settings, 'SPATIAL_SEMANTICS_CONFIG', {})
            model_path = config.get(
                'fasttext_model_path',
                os.path.join(settings.BASE_DIR, 'data/models/fasttext/cc.en.300.bin')
            )
            
            if not os.path.exists(model_path):
                logger.warning(f"FastText model not found at {model_path}. Creating mock for development.")
                cls._fasttext_model = cls._create_mock_fasttext()
            else:
                logger.info(f"Loading authoritative FastText model from {model_path}...")
                try:
                    cls._fasttext_model = fasttext.load_model(model_path)
                    logger.info("FastText model loaded into memory.")
                except Exception as e:
                    logger.error(f"Failed to load FastText: {e}")
                    cls._fasttext_model = cls._create_mock_fasttext()
                    
        return cls._fasttext_model

    @classmethod
    def _create_mock_fasttext(cls):
        """Provides a lightweight mock for environments without the 7GB binary."""
        class MockFastText:
            def get_word_vector(self, word):
                # Deterministic random vector for stable testing
                np.random.seed(hash(word) % (2**32))
                return np.random.randn(300).astype(np.float32)
            def get_dimension(self):
                return 300
        return MockFastText()

    @classmethod
    def unload_models(cls):
        """Force release of models from memory (useful for multiprocessing cleanup)."""
        cls._fasttext_model = None
        cls._nca_model = None
        import gc
        gc.collect()
        logger.info("Model registry cleared from memory.")
