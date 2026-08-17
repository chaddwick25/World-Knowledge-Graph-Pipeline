import json
from pathlib import Path
from typing import Dict, List

from django.core.management.base import BaseCommand
from django.conf import settings

from core.models import CountryPipelineProfile
from core.models import OSMWikiDataHierarchy
from core.services.planet_init.geofabrik_index_service import geofabrik_index_service
from core.services.planet_init.osm_wikidata_resolver import resolve_bbox_for_profile


class Command(BaseCommand):
    help = "Sync CountryPipelineProfile entries from embeddings + overrides + hierarchy metadata."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not write any changes to the database; only log what would happen.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional limit on number of countries to process (for testing).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options.get("limit")

        embeddings_root = Path(settings.EMBEDDINGS_ROOT) if getattr(settings, "EMBEDDINGS_ROOT", None) else None
        overrides_path = Path(settings.OVERRIDES_JSON_PATH) if getattr(settings, "OVERRIDES_JSON_PATH", None) else None

        if not embeddings_root or not embeddings_root.exists():
            self.stdout.write(self.style.ERROR("EMBEDDINGS_ROOT is not configured or does not exist."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"Scanning embeddings under {embeddings_root}..."))

        embedding_dirs: List[Path] = [p for p in embeddings_root.iterdir() if p.is_dir()]
        embedding_dirs.sort()
        if limit is not None:
            embedding_dirs = embedding_dirs[:limit]

        self.stdout.write(f"Discovered {len(embedding_dirs)} embedding country directories")

        # Load overrides.json if present
        overrides: Dict[str, dict] = {}
        if overrides_path and overrides_path.exists():
            try:
                with overrides_path.open("r") as f:
                    overrides = json.load(f)
                self.stdout.write(f"Loaded {len(overrides)} override records from {overrides_path}")
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"Failed to load overrides.json: {exc}"))
        else:
            self.stdout.write("No overrides.json found; proceeding without explicit overrides.")

        # Ensure Geofabrik index is available for enrichment (best-effort)
        try:
            geofabrik_index_service.fetch_index(force_refresh=False)
        except Exception:
            self.stdout.write(self.style.WARNING("Geofabrik index could not be loaded; continuing without Geofabrik enrichment."))

        created_count = 0
        updated_count = 0

        for country_dir in embedding_dirs:
            embedding_slug = country_dir.name
            rel_path = country_dir.relative_to(embeddings_root)

            # Minimal initial identity; enrichment is intentionally conservative here
            canonical_name = embedding_slug.replace("_", " ").replace("-", " ")
            canonical_slug = embedding_slug

            defaults = {
                "canonical_name": canonical_name,
                "canonical_slug": canonical_slug,
                "embedding_root_path": str(rel_path),
                "has_embeddings": True,
            }

            if dry_run:
                # Do not hit the database in dry-run mode so this command can be
                # exercised even before migrations for the new tables are applied.
                self.stdout.write(
                    f"[DRY-RUN] Would create or update CountryPipelineProfile for {embedding_slug}"
                )
                continue

            profile, created = CountryPipelineProfile.objects.update_or_create(
                embedding_slug=embedding_slug,
                defaults=defaults,
            )

            # Attempt to resolve and persist a BBOX for this profile using the
            # shared extraction-level resolver. This ties profiles to concrete
            # geometry (PolygonFile / PBF metrics / ISO fallback) without
            # introducing new schema in Phase 1.
            bbox = resolve_bbox_for_profile(profile)
            if bbox is not None:
                min_lon, min_lat, max_lon, max_lat = bbox
                payload = profile.country_relations_payload or {}
                payload["bbox"] = {
                    "min_lon": float(min_lon),
                    "min_lat": float(min_lat),
                    "max_lon": float(max_lon),
                    "max_lat": float(max_lat),
                }
                profile.country_relations_payload = payload
                profile.save(update_fields=["country_relations_payload"])
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"No BBOX resolved for CountryPipelineProfile {embedding_slug}; "
                        f"check country_poly / country_pbf / ISO codes."
                    )
                )

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"Created CountryPipelineProfile for {embedding_slug}"))
            else:
                updated_count += 1
                self.stdout.write(f"Updated CountryPipelineProfile for {embedding_slug}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Sync complete: {created_count} created, {updated_count} updated."))
