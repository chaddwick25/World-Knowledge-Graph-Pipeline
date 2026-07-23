import numpy as np
import types

import pytest

from geovectors_encoder.core.models.fasttext import FastTextModel


class DummyFastText:
    def __init__(self, dim=4):
        self._dim = dim
        self.calls = {}

    def get_dimension(self):
        return self._dim

    def get_word_vector(self, token):
        token = str(token)
        self.calls[token] = self.calls.get(token, 0) + 1
        # Return deterministic vector based on token for reproducibility
        base = float(len(token))
        return np.full(self._dim, base, dtype=np.float32)


@pytest.mark.unit
def test_fasttext_model_caches_repeated_tokens(monkeypatch):
    dummy = DummyFastText(dim=4)

    # Patch semantic_search ModelRegistryService so FastTextModel.__init__ uses our dummy
    import semantic_search.services.model_registry_service as mr_module

    class DummyRegistryService:
        @staticmethod
        def get_fasttext_model():
            return dummy

    monkeypatch.setattr(mr_module, "ModelRegistryService", DummyRegistryService)

    model = FastTextModel(model_path=None)

    tags = [
        ("name", "alpha beta"),
        ("highway", "alpha"),
        ("amenity", "beta"),
    ]

    # First call should populate cache
    v1 = model.encode_tags(tags)
    assert v1 is not None

    # Second call with same tags should hit cache for all tokens
    v2 = model.encode_tags(tags)
    assert v2 is not None

    # All vectors should be equal (deterministic dummy)
    np.testing.assert_allclose(v1, v2)

    # Tokens used: keys: name, highway, amenity; values: alpha, beta
    # Each should have exactly one underlying get_word_vector call
    expected_tokens = {"name", "highway", "amenity", "alpha", "beta"}
    assert set(dummy.calls.keys()) == expected_tokens
    for count in dummy.calls.values():
        assert count == 1
