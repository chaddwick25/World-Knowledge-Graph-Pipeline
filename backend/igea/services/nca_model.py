"""
Neural Class Alignment (NCA) Model

Implements the NCA component from the IGEA paper:
    Dsouza, Yu, Windoffer, Demidova — "Iterative Geographic Entity Alignment
    with Cross-Attention", ISWC 2023.

NCA learns tag-to-class mappings from linked OSM↔Wikidata entities.
It replaces the static ontology lookup with a neural model that:
  1. Encodes OSM tags as a multi-hot vector over known keys/values
  2. Projects into a shared latent space with KG class embeddings
  3. Predicts Wikidata classes via a classifier head
  4. Probes the trained model to extract (osm_tag, wikidata_class, confidence) triples

This is the PyTorch equivalent of the Keras SchemaModel in:
    papers/IGEA-master/scripts/schemaMatch.py

Architecture (matching SchemaModel.define_discriminator):
    Input (multi-hot tag vector) → Dense(100, relu) → Latent(dim) → Dense(dim, relu)
    → Dense(dim, relu) → Classifier(num_classes, sigmoid)

Usage:
    nca = NCAModel(latent_dim=64, num_classes=52)
    nca.train(tag_vectors, class_labels, epochs=50)
    mappings = nca.extract_tag_class_mappings(tag_vocab, class_vocab, threshold=0.25)
    # Returns: [('amenity=restaurant', 'wd:Q11707', 0.87), ...]
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
import torch
import torch.nn as nn
import torch.optim as optim
from collections import defaultdict

logger = logging.getLogger(__name__)

NCA_LATENT_DIM = 64
NCA_EPOCHS = 50
NCA_PREDICTION_THRESHOLD = 0.25  # IGEA paper default for Wikidata


class NCAModel(nn.Module):
    """
    Neural Class Alignment model — learns OSM tag → Wikidata class mappings.
    Matches the Keras SchemaModel from schemaMatch.py:
      Input → Dense(100, relu) → Latent(dim, relu) → Dense(dim, relu)
      → Dense(dim, relu) → Classifier(num_classes, sigmoid)
    """

    def __init__(self, input_dim: int, num_classes: int, latent_dim: int = NCA_LATENT_DIM):
        super().__init__()
        self.input_dim = input_dim
        self.num_classes = num_classes
        self.latent_dim = latent_dim

        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 100),
            nn.ReLU(),
            nn.Linear(100, latent_dim),
            nn.ReLU(),
        )
        self.classifier = nn.Sequential(
            nn.Linear(latent_dim, latent_dim),
            nn.ReLU(),
            nn.Linear(latent_dim, latent_dim),
            nn.ReLU(),
            nn.Linear(latent_dim, num_classes),
            nn.Sigmoid(),
        )

    def forward(self, x):
        latent = self.encoder(x)
        return self.classifier(latent), latent


class NCAClassificationService:
    """
    Service wrapping the NCA model for tag→class learning and inference.

    Lifecycle:
      1. build_vocabularies(linked_entities) — extract tag keys/values and Wikidata classes
      2. prepare_training_data(linked_entities) — create multi-hot vectors and labels
      3. train() — fit the neural model
      4. extract_mappings(threshold) — probe model to get (tag, class, confidence) triples
      5. apply_mappings(osm_entities) — assign wkg_class to entities using learned mappings
    """

    def __init__(
        self,
        latent_dim: int = NCA_LATENT_DIM,
        prediction_threshold: float = NCA_PREDICTION_THRESHOLD,
        epochs: int = NCA_EPOCHS,
    ):
        self.latent_dim = latent_dim
        self.prediction_threshold = prediction_threshold
        self.epochs = epochs
        self.model: Optional[NCAModel] = None
        self._tag_keys: List[str] = []
        self._tag_values: List[str] = []
        self._class_list: List[str] = []
        self._mappings: List[Tuple[str, str, float]] = []

    # ------------------------------------------------------------------
    # Vocabulary building
    # ------------------------------------------------------------------

    def build_vocabularies(self, linked_entities: List[Dict]) -> Tuple[List[str], List[str], List[str]]:
        """
        Extract vocabularies from linked OSM↔Wikidata entities.

        Args:
            linked_entities: list of dicts with:
                - 'tags': Dict[str, str]  (OSM tags)
                - 'wikidata_uri': str     (Wikidata class URI)

        Returns:
            (tag_keys, tag_values, class_list)
        """
        keys_set: Set[str] = set()
        values_set: Set[str] = set()
        classes_set: Set[str] = set()

        for ent in linked_entities:
            tags = ent.get('tags') or {}
            for k, v in tags.items():
                keys_set.add(k)
                values_set.add(v)
            wd_uri = ent.get('wikidata_uri')
            if wd_uri:
                classes_set.add(wd_uri)

        self._tag_keys = sorted(keys_set)
        self._tag_values = sorted(values_set)
        self._class_list = sorted(classes_set)

        logger.info(
            f"NCA vocabularies: {len(self._tag_keys)} keys, "
            f"{len(self._tag_values)} values, {len(self._class_list)} classes"
        )
        return self._tag_keys, self._tag_values, self._class_list

    # ------------------------------------------------------------------
    # Training data preparation
    # ------------------------------------------------------------------

    def _encode_tags(self, tags: Dict[str, str]) -> np.ndarray:
        """
        Encode OSM tags as a multi-hot vector over key+value vocabulary.

        Vector = [key_bits | value_bits] where each bit is 1 if the
        key/value appears in the entity's tags.
        """
        vec = np.zeros(len(self._tag_keys) + len(self._tag_values), dtype=np.float32)
        for k, v in tags.items():
            if k in self._tag_keys:
                vec[self._tag_keys.index(k)] = 1.0
            if v in self._tag_values:
                vec[len(self._tag_keys) + self._tag_values.index(v)] = 1.0
        return vec

    def _encode_class(self, wikidata_uri: str) -> np.ndarray:
        """Encode Wikidata class as a one-hot vector."""
        label = np.zeros(len(self._class_list), dtype=np.float32)
        if wikidata_uri in self._class_list:
            label[self._class_list.index(wikidata_uri)] = 1.0
        return label

    def prepare_training_data(
        self, linked_entities: List[Dict]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Create training tensors from linked entities.

        Returns:
            (X, y) where X is (N, input_dim) and y is (N, num_classes)
        """
        if not self._tag_keys or not self._class_list:
            raise ValueError("Vocabularies not built. Call build_vocabularies() first.")

        X_list, y_list = [], []
        for ent in linked_entities:
            tags = ent.get('tags') or {}
            wd_uri = ent.get('wikidata_uri')
            if not wd_uri:
                continue
            X_list.append(self._encode_tags(tags))
            y_list.append(self._encode_class(wd_uri))

        if not X_list:
            raise ValueError("No valid training examples found.")

        X = torch.tensor(np.stack(X_list), dtype=torch.float32)
        y = torch.tensor(np.stack(y_list), dtype=torch.float32)
        logger.info(f"NCA training data: {X.shape[0]} examples, {X.shape[1]} features, {y.shape[1]} classes")
        return X, y

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        X: torch.Tensor,
        y: torch.Tensor,
        learning_rate: float = 1e-4,
        verbose: bool = True,
    ) -> NCAModel:
        """
        Train the NCA model on multi-hot tag vectors → class labels.

        Uses binary cross-entropy loss (multi-label classification).
        """
        input_dim = X.shape[1]
        num_classes = y.shape[1]

        self.model = NCAModel(input_dim, num_classes, self.latent_dim)
        optimizer = optim.Adam(self.model.parameters(), lr=learning_rate)
        criterion = nn.BCELoss()

        self.model.train()
        for epoch in range(self.epochs):
            optimizer.zero_grad()
            predictions, _ = self.model(X)
            loss = criterion(predictions, y)
            loss.backward()
            optimizer.step()

            if verbose and (epoch + 1) % 10 == 0:
                logger.info(f"NCA epoch {epoch + 1}/{self.epochs} | loss={loss.item():.4f}")

        self.model.eval()
        logger.info(f"NCA training complete: final loss={loss.item():.4f}")
        return self.model

    # ------------------------------------------------------------------
    # Mapping extraction (probe the model)
    # ------------------------------------------------------------------

    def extract_mappings(
        self,
        threshold: Optional[float] = None,
    ) -> List[Tuple[str, str, float]]:
        """
        Probe the trained NCA model to extract (tag, class, confidence) triples.

        For each unique tag key+value combination in the vocabulary, we:
          1. Create a one-hot input vector for that single tag
          2. Run through the model
          3. Record all class predictions above threshold

        This mirrors the getMatches() function in schemaMatch.py.

        Returns:
            List of (osm_tag, wikidata_class_uri, confidence) sorted by confidence desc.
        """
        if self.model is None:
            logger.warning("NCA model not trained. Call train() first.")
            return []

        threshold = threshold or self.prediction_threshold
        mappings = []

        self.model.eval()
        with torch.no_grad():
            # Probe each tag key
            for key in self._tag_keys:
                vec = np.zeros(len(self._tag_keys) + len(self._tag_values), dtype=np.float32)
                vec[self._tag_keys.index(key)] = 1.0
                tensor = torch.tensor(vec, dtype=torch.float32).unsqueeze(0)
                predictions, _ = self.model(tensor)
                probs = predictions.squeeze(0).numpy()
                for cls_idx, prob in enumerate(probs):
                    if prob >= threshold:
                        mappings.append((key, self._class_list[cls_idx], float(prob)))

            # Probe each tag value
            for val in self._tag_values:
                vec = np.zeros(len(self._tag_keys) + len(self._tag_values), dtype=np.float32)
                vec[len(self._tag_keys) + self._tag_values.index(val)] = 1.0
                tensor = torch.tensor(vec, dtype=torch.float32).unsqueeze(0)
                predictions, _ = self.model(tensor)
                probs = predictions.squeeze(0).numpy()
                for cls_idx, prob in enumerate(probs):
                    if prob >= threshold:
                        mappings.append((f"={val}", self._class_list[cls_idx], float(prob)))

        mappings.sort(key=lambda x: x[2], reverse=True)
        self._mappings = mappings
        logger.info(f"NCA extracted {len(mappings)} tag→class mappings (threshold={threshold})")
        return mappings

    # ------------------------------------------------------------------
    # Apply learned mappings to entities
    # ------------------------------------------------------------------

    def predict_class(self, tags: Dict[str, str]) -> Optional[str]:
        """
        Predict Wikidata class for an OSM entity using the trained NCA model.

        Falls back to the extracted mappings if the model isn't available.
        """
        if self.model is not None:
            self.model.eval()
            with torch.no_grad():
                vec = self._encode_tags(tags)
                tensor = torch.tensor(vec, dtype=torch.float32).unsqueeze(0)
                predictions, _ = self.model(tensor)
                probs = predictions.squeeze(0).numpy()
                best_idx = int(np.argmax(probs))
                if probs[best_idx] >= self.prediction_threshold:
                    return self._class_list[best_idx]
                return None

        # Fallback: use extracted mappings
        for tag_key, wd_class, conf in self._mappings:
            if tag_key in tags:
                return wd_class
            if tag_key.startswith('=') and tag_key[1:] in tags.values():
                return wd_class

        return None

    def apply_mappings_to_entities(
        self,
        osm_entities: List[Dict],
    ) -> int:
        """
        Assign wkg_class to OSM entities using NCA-learned mappings.

        Args:
            osm_entities: list of dicts with 'tags' key, mutated in-place with 'wkg_class'.

        Returns:
            Number of entities assigned a class.
        """
        assigned = 0
        for ent in osm_entities:
            tags = ent.get('tags') or {}
            if ent.get('wkg_class'):
                continue  # Already has a class
            predicted = self.predict_class(tags)
            if predicted:
                ent['wkg_class'] = predicted
                assigned += 1

        logger.info(f"NCA assigned classes to {assigned}/{len(osm_entities)} entities")
        return assigned

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Save model weights and vocabularies."""
        if self.model is None:
            raise ValueError("No model to save.")
        state = {
            'model_state': self.model.state_dict(),
            'input_dim': self.model.input_dim,
            'num_classes': self.model.num_classes,
            'latent_dim': self.latent_dim,
            'tag_keys': self._tag_keys,
            'tag_values': self._tag_values,
            'class_list': self._class_list,
            'mappings': self._mappings,
        }
        torch.save(state, path)
        logger.info(f"NCA model saved to {path}")

    def load(self, path: str) -> bool:
        """Load model weights and vocabularies."""
        try:
            state = torch.load(path, map_location=torch.device('cpu'))
            self._tag_keys = state['tag_keys']
            self._tag_values = state['tag_values']
            self._class_list = state['class_list']
            self._mappings = state.get('mappings', [])
            self.latent_dim = state['latent_dim']

            self.model = NCAModel(
                state['input_dim'], state['num_classes'], state['latent_dim']
            )
            self.model.load_state_dict(state['model_state'])
            self.model.eval()
            logger.info(f"NCA model loaded from {path} ({len(self._mappings)} mappings)")
            return True
        except Exception as e:
            logger.error(f"Failed to load NCA model: {e}")
            return False
