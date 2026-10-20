"""DB-driven amenity → WorldKG class mappings (rule 6.2, hardening Phase 5).

Populates ``factor_amenity_class_mapping`` (vectors DB) from two tiers:

- Tier 1 (data): aggregate ``OsmEntity`` rows carrying an ``amenity`` tag,
  group by amenity value, take the most common ``wkg_class`` with its
  support count; ``source='data'``.
- Tier 2 (ontology): parse the WorldKG ontology TTL (same source as
  ``test_worldkg_namespaces.py`` / ``OntologyLoader.load_from_ttl``) and add
  exact ``canonical_osm_value → wkg_class`` mappings for amenity-keyed
  classes not covered by the data tier; ``source='ontology'``.

Idempotent: delete-then-insert for the whole table (mirrors
``FactorNodeWriter`` / Step 5c).

Runtime consumer: ``FactorResolutionService.amenity_class()`` — replaces the
hardcoded ``amenity_to_wkgs`` dicts in ``QueryExecutorService``
(``_amenity_candidate_osm_ids`` + the ontology-class search path).

Usage::

    python manage.py compute_amenity_class_mappings
    python manage.py compute_amenity_class_mappings --dry-run
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Populate factor_amenity_class_mapping (vectors DB) from OsmEntity "
        "aggregation + the WorldKG ontology TTL."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print the mappings that would be written without touching "
                 "the DB.",
        )

    def handle(self, *args, **options):
        from worldkg_nca.models import AmenityClassMapping, OsmEntity

        dry_run = options["dry_run"]

        # ── Tier 1: data aggregation ─────────────────────────────────────
        agg = (
            OsmEntity.objects.using("vectors")
            .filter(
                wkg_class__isnull=False,
                tags__amenity__isnull=False,
            )
            .exclude(wkg_class="")
            .exclude(tags__amenity="")
            .values("tags__amenity", "wkg_class")
            .annotate(cnt=Count("id"))
            .order_by("tags__amenity", "-cnt")
        )
        data_mappings = []  # (amenity, wkg_class, support_count)
        seen = set()
        for row in agg:
            amenity = (row["tags__amenity"] or "").strip().lower().replace(" ", "_")
            if not amenity or amenity in seen:
                continue
            seen.add(amenity)
            data_mappings.append((amenity, row["wkg_class"], row["cnt"]))
        self.stdout.write(f"Tier 1 (data): {len(data_mappings)} amenity values")

        # ── Tier 2: ontology TTL ─────────────────────────────────────────
        ontology_mappings = {}
        ttl_path = Path(getattr(settings, "WORLDKG_ONTOLOGY_PATH", "") or "")
        if ttl_path.exists():
            from worldkg_nca.services.ontology_loader import WorldKGOntologyLoader

            ontology_dict = WorldKGOntologyLoader().load_from_ttl(str(ttl_path))
            for class_name, meta in ontology_dict.items():
                if (
                    meta.get("canonical_osm_key") == "amenity"
                    and meta.get("canonical_osm_value")
                ):
                    value = (
                        meta["canonical_osm_value"].strip().lower()
                        .replace(" ", "_")
                    )
                    if value:
                        ontology_mappings.setdefault(value, class_name)
            self.stdout.write(
                f"Tier 2 (ontology): {len(ontology_mappings)} amenity values"
            )
        else:
            self.stdout.write(self.style.WARNING(
                f"WorldKG ontology TTL not found at {ttl_path} — "
                "ontology tier skipped"
            ))

        # ── Precedence: data wins on support; ontology fills the rest ──
        total = len(data_mappings)
        for amenity, wkg_class in ontology_mappings.items():
            if amenity not in seen:
                data_mappings.append((amenity, wkg_class, None))
                total += 1

        if dry_run:
            for amenity, wkg_class, cnt in data_mappings:
                src = "data" if cnt is not None else "ontology"
                suffix = f" (support={cnt})" if cnt is not None else ""
                self.stdout.write(f"  {amenity} -> {wkg_class} [{src}]{suffix}")
            self.stdout.write(f"dry-run: {total} mappings (not written)")
            return

        # ── Idempotent write (delete-then-insert) ────────────────────────
        AmenityClassMapping.objects.using("vectors").all().delete()
        objs = [
            AmenityClassMapping(
                amenity_text=amenity,
                wkg_class=wkg_class,
                support_count=cnt,
                source="data" if cnt is not None else "ontology",
            )
            for amenity, wkg_class, cnt in data_mappings
        ]
        AmenityClassMapping.objects.using("vectors").bulk_create(
            objs, batch_size=1000,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {total} AmenityClassMapping rows (factor_amenity_class_mapping)"
        ))
