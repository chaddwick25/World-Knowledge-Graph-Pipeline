import fasttext
import fasttext.util
import ast
import numpy as np
from .base import BaseModel

"""
This is class represents the Fasttext embedding model for OpenStreetMap tags.
"""

class FastTextModel(BaseModel):

    def __init__(self, model_path=None):
        BaseModel.__init__(self)
        if model_path is None:
            from semantic_search.services.model_registry_service import ModelRegistryService
            self.ft = ModelRegistryService.get_fasttext_model()
        else:
            self.ft = fasttext.load_model(model_path)
        self.dimension = self.ft.get_dimension()
        self._token_cache = {}

    def train(self, data):
        pass

    def encode_pandas_instance(self, instance):
        tags = ast.literal_eval(instance[1]['tags'])
        return self.encode_tags(tags)

    def encode_instance(self, instance):
        return self.encode_tags(instance[2])

    def encode_tags(self, tags):
        if len(tags) == 0:
            return None
        else:
            vectors = []
            for k, v in tags:
                # Always include the key (e.g., 'name', 'highway')
                vectors.append(self._get_word_vector_cached(str(k)))
                
                # Tokenize value and include all words
                if v:
                    val_str = str(v)
                    for token in val_str.split():
                        if token:
                            vectors.append(self._get_word_vector_cached(token))

            if not vectors:
                return None
                
            enc = np.mean(vectors, axis=0)
            
            # Apply L2 Normalization to ensure manifold consistency with Search API
            norm = np.linalg.norm(enc)
            if norm > 0:
                enc = enc / norm
        return enc

    def _get_word_vector_cached(self, token):
        token_str = str(token)
        cached = self._token_cache.get(token_str)
        if cached is not None:
            return cached
        vec = self.ft.get_word_vector(token_str)
        self._token_cache[token_str] = vec
        return vec

    def save_model(self, path):
        pass



