import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import CountryPipelineProfile, SubgraphProfile


class Command(BaseCommand):
    help = "Export country and subgraph configuration metrics for IGEA coverage analysis.\n\n" \
           "The output CSV is designed to be consumed by analysis notebooks in papers/notebooks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help=(
                "Optional output CSV path. "
                "Defaults to <project-root>/data/igea_config_metrics.csv."
            ),
        )

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR).parent
        default_output = project_root / "data" / "igea_config_metrics.csv"
        output_path = Path(options["output"]) if options["output"] else default_output
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Load legacy country_relations.json overlay if present
        country_relations_path = project_root / "data" / "country_relations.json"
        legacy_relations = {}
        if country_relations_path.exists():
            try:
                with open(country_relations_path, "r", encoding="utf-8") as f:
                    legacy_relations = json.load(f)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Loaded {len(legacy_relations)} entries from {country_relations_path}"
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.stdout.write(
                    self.style.WARNING(
                        f"Failed to load {country_relations_path}: {exc}"
                    )
                )

        # Build quick lookup of which ISO codes already have profiles
        profiles = (
            CountryPipelineProfile.objects.all()
            .prefetch_related("subgraphs")
            .order_by("canonical_name")
        )

        rows = []
        seen_iso_keys = set()

        for profile in profiles:
            iso2 = (profile.iso2 or "").upper()
            iso3 = (profile.iso3 or "").upper()
            if iso2:
                seen_iso_keys.add(("ISO2", iso2))
            if iso3:
                seen_iso_keys.add(("ISO3", iso3))

            subgraphs = list(profile.subgraphs.all())
            total_subgraphs = len(subgraphs)
            subgraphs_with_pbf = sum(1 for sg in subgraphs if sg.has_subgraph_pbf)
            subgraphs_with_poly = sum(1 for sg in subgraphs if sg.has_subgraph_poly)
            subgraphs_with_pickle = sum(1 for sg in subgraphs if sg.has_subgraph_pickle)

            # Approximate IGEA feasibility: requires embeddings + non-zero nodes.
            #
            # Under the shared-pickle model, a **single country-level** wdw.pickle
            # is canonical; subgraphs are used purely for geofencing. Missing
            # subgraph pickles must NOT block IGEA. If subgraphs are present, we
            # only require that at least one subgraph has a usable poly (so that
            # we can geofence work even if some subgraphs are still being wired
            # to hierarchy/relations).
            has_embeddings = bool(profile.has_embeddings)
            has_osm_nodes = (profile.node_count or 0) > 0
            if total_subgraphs:
                has_geofenced_subgraph = subgraphs_with_poly > 0
            else:
                # Small territories / no subgraphs: country-level geofence only.
                has_geofenced_subgraph = True

            igea_likely_supported = bool(
                has_embeddings and has_osm_nodes and has_geofenced_subgraph
            )

            # Legacy JSON presence (by ISO2/ISO3)
            legacy_entry = None
            if iso2 and iso2 in legacy_relations:
                legacy_entry = legacy_relations[iso2]
            elif iso3 and iso3 in legacy_relations:
                legacy_entry = legacy_relations[iso3]

            row = {
                "source": "db",  # from CountryPipelineProfile
                "iso2": iso2,
                "iso3": iso3,
                "canonical_name": profile.canonical_name,
                "canonical_slug": profile.canonical_slug,
                "continent_name": profile.continent_name or "",
                "has_embeddings": has_embeddings,
                "node_count": profile.node_count or 0,
                "way_count": profile.way_count or 0,
                "relation_count": profile.relation_count or 0,
                "file_size_bytes": profile.file_size_bytes or 0,
                "has_subgraphs_flag": bool(profile.has_subgraphs),
                "subgraphs_total": total_subgraphs,
                "subgraphs_with_pbf": subgraphs_with_pbf,
                "subgraphs_with_poly": subgraphs_with_poly,
                "subgraphs_with_pickle": subgraphs_with_pickle,
                "subgraphs_pickle_coverage": (
                    subgraphs_with_pickle / total_subgraphs
                    if total_subgraphs
                    else 0.0
                ),
                "country_relations_present": bool(legacy_entry),
                "metadata_status": profile.metadata_status,
                "igea_likely_supported": igea_likely_supported,
            }
            rows.append(row)

        # Add JSON-only entries for regions that exist in country_relations.json
        # but have no CountryPipelineProfile. These are structural holes that
        # cannot currently enter the pipeline at all.
        for iso_code, payload in legacy_relations.items():
            iso_norm = (iso_code or "").upper()
            key_pair_iso2 = ("ISO2", iso_norm)
            key_pair_iso3 = ("ISO3", iso_norm)
            if key_pair_iso2 in seen_iso_keys or key_pair_iso3 in seen_iso_keys:
                continue

            rows.append(
                {
                    "source": "json_only",
                    "iso2": iso_norm if len(iso_norm) == 2 else "",
                    "iso3": iso_norm if len(iso_norm) == 3 else "",
                    "canonical_name": payload.get("name") or iso_norm,
                    "canonical_slug": payload.get("slug") or "",
                    "continent_name": payload.get("continent_name") or "",
                    "has_embeddings": False,
                    "node_count": 0,
                    "way_count": 0,
                    "relation_count": 0,
                    "file_size_bytes": 0,
                    "has_subgraphs_flag": False,
                    "subgraphs_total": 0,
                    "subgraphs_with_pbf": 0,
                    "subgraphs_with_poly": 0,
                    "subgraphs_with_pickle": 0,
                    "subgraphs_pickle_coverage": 0.0,
                    "country_relations_present": True,
                    "metadata_status": "MISSING_COUNTRY_PROFILE",
                    "igea_likely_supported": False,
                }
            )

        if not rows:
            self.stdout.write(self.style.WARNING("No configuration rows found to export."))
            return

        fieldnames = [
            "source",
            "iso2",
            "iso3",
            "canonical_name",
            "canonical_slug",
            "continent_name",
            "has_embeddings",
            "node_count",
            "way_count",
            "relation_count",
            "file_size_bytes",
            "has_subgraphs_flag",
            "subgraphs_total",
            "subgraphs_with_pbf",
            "subgraphs_with_poly",
            "subgraphs_with_pickle",
            "subgraphs_pickle_coverage",
            "country_relations_present",
            "metadata_status",
            "igea_likely_supported",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(rows)} rows to {output_path}")
        )
