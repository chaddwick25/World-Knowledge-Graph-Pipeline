import os
from pathlib import Path
import argparse
import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader


def collect_training_pairs(sample_size: int = 10000, min_tags: int = 1, force_cpu: bool = False):
    """Collect (SBERT 384D, FastText 300D) pairs from OsmEntity.

    Uses existing GV-Tags embeddings from the vectors DB as the FastText
    "teacher" target and recomputes SBERT embeddings from OSM tags.
    """
    from django.conf import settings
    from worldkg_nca.models import OsmEntity
    from semantic_search.services.fasttext_service import FastTextEmbeddingService
    from sentence_transformers import SentenceTransformer

    # Determine device and load SBERT model
    if force_cpu:
        device = torch.device("cpu")
    else:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model_name = "all-MiniLM-L6-v2"
    print(f"[collect] Loading SBERT model {model_name} on {device}...")
    sbert_model = SentenceTransformer(model_name, device=device)

    # Query OsmEntity records with gv_tags_embedding present
    qs = (
        OsmEntity.objects.using("vectors")
        .filter(gv_tags_embedding__isnull=False)
        .only("id", "tags", "gv_tags_embedding")[:sample_size]
    )

    if not qs:
        raise RuntimeError("No OsmEntity rows with gv_tags_embedding found in vectors DB.")

    print(f"[collect] Fetched {len(qs)} OsmEntity rows from vectors DB")

    texts = []
    fasttext_vectors = []

    for entity in qs:
        tags = entity.tags or {}
        if not isinstance(tags, dict) or len(tags) < min_tags:
            continue

        # Build tag_counts using the same helper used at inference time
        tag_counts = FastTextEmbeddingService.build_tag_counts_from_osm_tags(tags)
        if not tag_counts:
            continue

        # Teacher embedding (FastText) from stored gv_tags_embedding
        gv_vec = entity.gv_tags_embedding
        if gv_vec is None:
            continue

        gv_vec = np.asarray(gv_vec, dtype=np.float32)
        if gv_vec.shape != (300,):
            # Skip malformed vectors
            continue

        # Build SBERT input text by repeating tags according to counts,
        # mirroring SBERTEmbeddingService.encode_tags behavior.
        parts = []
        for tag, count in tag_counts.items():
            parts.extend([tag] * int(count))
        if not parts:
            continue

        text = " ".join(parts)
        texts.append(text)
        fasttext_vectors.append(gv_vec)

    if not texts:
        raise RuntimeError("No valid training pairs collected from OsmEntity.")

    print(f"[collect] Built {len(texts)} training examples; encoding with SBERT...")

    # Encode all texts with SBERT to obtain 384D embeddings
    with torch.no_grad():
        sbert_embeddings = sbert_model.encode(
            texts,
            batch_size=64,
            show_progress_bar=True,
            convert_to_numpy=True,
        )

    sbert_embeddings = np.asarray(sbert_embeddings, dtype=np.float32)
    fasttext_array = np.stack(fasttext_vectors).astype(np.float32)

    if sbert_embeddings.shape[0] != fasttext_array.shape[0]:
        raise RuntimeError("Mismatch between SBERT and FastText sample counts.")

    print(
        f"[collect] SBERT embeddings shape: {sbert_embeddings.shape}, "
        f"FastText embeddings shape: {fasttext_array.shape}"
    )

    return sbert_embeddings, fasttext_array


def split_train_val_test(sbert_emb: np.ndarray, fasttext_emb: np.ndarray, seed: int = 42):
    """Split data into train/val/test (80/10/10)."""
    assert sbert_emb.shape[0] == fasttext_emb.shape[0]
    n = sbert_emb.shape[0]
    indices = np.arange(n)
    rng = np.random.default_rng(seed)
    rng.shuffle(indices)

    train_end = int(0.8 * n)
    val_end = int(0.9 * n)

    train_idx = indices[:train_end]
    val_idx = indices[train_end:val_end]
    test_idx = indices[val_end:]

    def _slice(arr, idx):
        return arr[idx]

    return (
        _slice(sbert_emb, train_idx),
        _slice(fasttext_emb, train_idx),
        _slice(sbert_emb, val_idx),
        _slice(fasttext_emb, val_idx),
        _slice(sbert_emb, test_idx),
        _slice(fasttext_emb, test_idx),
    )


class ProjectionDataset(Dataset):
    def __init__(self, sbert_emb: np.ndarray, fasttext_emb: np.ndarray):
        assert sbert_emb.shape[0] == fasttext_emb.shape[0]
        self.sbert = torch.from_numpy(sbert_emb).float()
        self.fasttext = torch.from_numpy(fasttext_emb).float()

    def __len__(self):
        return self.sbert.shape[0]

    def __getitem__(self, idx):
        return {
            "sbert": self.sbert[idx],
            "fasttext": self.fasttext[idx],
        }


class ProjectionHeadTrainer:
    def __init__(self, input_dim: int = 384, output_dim: int = 300, device: str = "cuda:0"):
        self.device = torch.device(device)
        self.projection = nn.Linear(input_dim, output_dim).to(self.device)
        self.optimizer = torch.optim.Adam(
            self.projection.parameters(), lr=1e-3, weight_decay=1e-5
        )
        self.criterion = nn.MSELoss()

    def train_epoch(self, train_loader: DataLoader) -> float:
        self.projection.train()
        total_loss = 0.0

        for batch in train_loader:
            sbert_emb = batch["sbert"].to(self.device)
            fasttext_emb = batch["fasttext"].to(self.device)

            projected = self.projection(sbert_emb)
            loss = self.criterion(projected, fasttext_emb)

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            total_loss += loss.item()

        return total_loss / max(len(train_loader), 1)

    def validate(self, val_loader: DataLoader) -> float:
        self.projection.eval()
        total_loss = 0.0

        with torch.no_grad():
            for batch in val_loader:
                sbert_emb = batch["sbert"].to(self.device)
                fasttext_emb = batch["fasttext"].to(self.device)

                projected = self.projection(sbert_emb)
                loss = self.criterion(projected, fasttext_emb)
                total_loss += loss.item()

        return total_loss / max(len(val_loader), 1)


def resolve_paths():
    """Resolve data directory and projection head path from Django settings."""
    from django.conf import settings

    base_data = settings.BASE_DATA_DIR or str(settings.BASE_DIR / "data")
    base_path = Path(base_data)

    training_dir = base_path / "projection_training"
    training_dir.mkdir(parents=True, exist_ok=True)

    # Prefer SBERT_PROJECTION_PATH if defined; otherwise default under models/
    projection_path = getattr(settings, "SBERT_PROJECTION_PATH", None)
    if not projection_path:
        projection_path = str(base_path / "models" / "sbert_projection_head.pt")

    return training_dir, Path(projection_path)


def main():
    import django
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Train SBERT→FastText projection head.")
    parser.add_argument("--sample-size", type=int, default=10000, help="Number of OsmEntity rows to sample")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=256, help="Batch size")
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU mode even if CUDA is available")

    args = parser.parse_args()

    # When run as a standalone script/module, ensure Django is initialized
    # Add project root to Python path
    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    django.setup()

    from django.conf import settings

    training_dir, projection_path = resolve_paths()
    print(f"[paths] Training data dir: {training_dir}")
    print(f"[paths] Projection head path: {projection_path}")

    # Phase 1: Data collection
    sbert_emb, fasttext_emb = collect_training_pairs(sample_size=args.sample_size, force_cpu=args.force_cpu)

    (
        train_sbert,
        train_fasttext,
        val_sbert,
        val_fasttext,
        test_sbert,
        test_fasttext,
    ) = split_train_val_test(sbert_emb, fasttext_emb)

    npz_path = training_dir / "training_pairs.npz"
    np.savez(
        npz_path,
        train_sbert=train_sbert,
        train_fasttext=train_fasttext,
        val_sbert=val_sbert,
        val_fasttext=val_fasttext,
        test_sbert=test_sbert,
        test_fasttext=test_fasttext,
    )
    print(f"[data] Saved training pairs to {npz_path}")

    # Phase 2: Model training
    if args.force_cpu:
        device = "cpu"
    else:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"[train] Using device: {device}")

    train_dataset = ProjectionDataset(train_sbert, train_fasttext)
    val_dataset = ProjectionDataset(val_sbert, val_fasttext)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

    trainer = ProjectionHeadTrainer(device=device)
    best_val_loss = float("inf")

    for epoch in range(1, args.epochs + 1):
        train_loss = trainer.train_epoch(train_loader)
        val_loss = trainer.validate(val_loader)

        print(
            f"[epoch {epoch:03d}] train_loss={train_loss:.6f} "
            f"val_loss={val_loss:.6f}"
        )

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            projection_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(trainer.projection.state_dict(), projection_path)
            print(
                f"[checkpoint] New best val_loss={best_val_loss:.6f}; "
                f"saved to {projection_path}"
            )

    print("[done] Training complete.")
    print(f"Best validation loss: {best_val_loss:.6f}")
    print(f"Projection head weights saved to: {projection_path}")


if __name__ == "__main__":
    main()
