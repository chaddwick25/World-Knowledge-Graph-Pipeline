"""
Django management command: Train SBERT Projection Head

Trains a projection head to align SBERT embeddings (384D) with FastText
embeddings (300D) at country or subgraph level, following the GV-NLE pattern.
"""

from django.core.management.base import BaseCommand
from django.conf import settings
import logging
import argparse
from pathlib import Path
import numpy as np
import torch
from torch.utils.data import DataLoader

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Train SBERT projection head for country or subgraph'

    def add_arguments(self, parser):
        parser.add_argument(
            '--region',
            type=str,
            required=True,
            help='Region slug (country or subgraph) for training'
        )
        example_base = getattr(
            settings,
            'OSM_WIKIDATA_EXTRACTIONS_DIR',
            '/path/to/osm_wikidata_extractions',
        )
        parser.add_argument(
            '--base-path',
            type=str,
            required=True,
            help=f'Base path for the country (e.g., {example_base}/central_america/belize)'
        )
        parser.add_argument(
            '--sample-size',
            type=int,
            default=10000,
            help='Number of OsmEntity rows to sample for training'
        )
        parser.add_argument(
            '--epochs',
            type=int,
            default=50,
            help='Training epochs'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=256,
            help='Batch size'
        )
        parser.add_argument(
            '--force-cpu',
            action='store_true',
            help='Force CPU mode even if CUDA is available'
        )

    def handle(self, *args, **options):
        region = options['region']
        base_path = options['base_path']
        sample_size = options['sample_size']
        epochs = options['epochs']
        batch_size = options['batch_size']
        force_cpu = options['force_cpu']

        self.stdout.write(f"Training SBERT projection head for region: {region}")
        self.stdout.write(f"Base path: {base_path}")

        # Import training functions
        from semantic_search.training.train_projection_head import (
            collect_training_pairs,
            split_train_val_test,
            ProjectionDataset,
            ProjectionHeadTrainer,
        )

        # Determine device
        if force_cpu:
            device = "cpu"
        else:
            device = "cuda:0" if torch.cuda.is_available() else "cpu"

        self.stdout.write(f"Using device: {device}")

        # Collect training data
        self.stdout.write(f"Collecting {sample_size} training pairs...")
        sbert_emb, fasttext_emb = collect_training_pairs(
            sample_size=sample_size, force_cpu=force_cpu
        )

        # Split data
        self.stdout.write("Splitting data into train/val/test...")
        (
            train_sbert,
            train_fasttext,
            val_sbert,
            val_fasttext,
            _,
            _,
        ) = split_train_val_test(sbert_emb, fasttext_emb)

        # Create datasets
        train_dataset = ProjectionDataset(train_sbert, train_fasttext)
        val_dataset = ProjectionDataset(val_sbert, val_fasttext)

        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

        # Determine output path following subgraph directory structure
        # {base_path}/projection_head/{region}/projection_head.pt
        base_path = Path(base_path)
        projection_dir = base_path / "projection_head" / region
        projection_dir.mkdir(parents=True, exist_ok=True)
        projection_path = projection_dir / "projection_head.pt"

        self.stdout.write(f"Output path: {projection_path}")

        # Train
        self.stdout.write(f"Training for {epochs} epochs...")
        trainer = ProjectionHeadTrainer(device=device)
        best_val_loss = float("inf")

        for epoch in range(1, epochs + 1):
            train_loss = trainer.train_epoch(train_loader)
            val_loss = trainer.validate(val_loader)

            self.stdout.write(
                f"Epoch {epoch:03d}: train_loss={train_loss:.6f}, val_loss={val_loss:.6f}"
            )

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                projection_path.parent.mkdir(parents=True, exist_ok=True)
                torch.save(trainer.projection.state_dict(), projection_path)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  New best val_loss={best_val_loss:.6f}; saved to {projection_path}"
                    )
                )

        self.stdout.write(self.style.SUCCESS("Training complete."))
        self.stdout.write(f"Best validation loss: {best_val_loss:.6f}")
        self.stdout.write(f"Projection head saved to: {projection_path}")
