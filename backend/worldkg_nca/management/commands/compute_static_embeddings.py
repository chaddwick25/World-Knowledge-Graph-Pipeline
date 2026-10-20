from django.core.management.base import BaseCommand
from worldkg_nca.models import OsmEntity
import numpy as np


class Command(BaseCommand):
    help = "Compute static_embedding for OsmEntity by concatenating GV-Tags (300D) + GV-NLE (100D) = 400D."

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Computing static_embedding for OsmEntity (GV-Tags + GV-NLE = 400D)..."))

        batch_size = 5000
        updated_count = 0

        # Query entities that have both embeddings but no static_embedding.
        # IMPORTANT: do NOT use offset-based slicing (qs[i:i+batch_size]) here.
        # As each entity is updated its static_embedding becomes non-null, so it
        # drops out of the queryset — causing offset-based slices to skip rows.
        # Use .iterator() to stream all candidates in a single pass instead.
        qs = OsmEntity.objects.using('vectors').filter(
            gv_tags_embedding__isnull=False,
            gv_nle_embedding__isnull=False,
            static_embedding__isnull=True
        ).only('id', 'gv_tags_embedding', 'gv_nle_embedding')

        total = qs.count()
        self.stdout.write(f"Found {total} entities to update.")

        batch = []
        for entity in qs.iterator(chunk_size=batch_size):
            if entity.gv_tags_embedding is not None and entity.gv_nle_embedding is not None:
                tags = np.array(entity.gv_tags_embedding)
                nle = np.array(entity.gv_nle_embedding)
                entity.static_embedding = np.concatenate([tags, nle]).tolist()
                batch.append(entity)

            if len(batch) >= batch_size:
                OsmEntity.objects.using('vectors').bulk_update(batch, ['static_embedding'])
                updated_count += len(batch)
                self.stdout.write(f"Updated {updated_count}/{total} entities...")
                batch = []

        # Flush remaining entities
        if batch:
            OsmEntity.objects.using('vectors').bulk_update(batch, ['static_embedding'])
            updated_count += len(batch)

        self.stdout.write(self.style.SUCCESS(f"Computed static_embedding for {updated_count} entities."))
