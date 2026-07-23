import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from orchestration.models import CountryPipelineProfile


class Command(BaseCommand):
    help = (
        "Export per-subgraph coverage metrics for IGEA/USLP readiness analysis.\n\n"
        "The output CSV is designed to be consumed by analysis notebooks in "
        "papers/notebooks and by frontend metrics endpoints."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help=(
                "Optional output CSV path. "
                "Defaults to <project-root>/data/subgraph_coverage_metrics.csv."
            ),
        )

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR).parent
        default_output = project_root / "data" / "subgraph_coverage_metrics.csv"
        output_path = Path(options["output"]) if options["output"] else default_output
        output_path.parent.mkdir(parents=True, exist_ok=True)

        profiles = (
            CountryPipelineProfile.objects.all()
            .prefetch_related("subgraphs")
            .order_by("canonical_name")
        )

        rows = []

        for profile in profiles:
            iso2 = (profile.iso2 or "").upper()
            iso3 = (profile.iso3 or "").upper()

            for sg in profile.subgraphs.all():
                # Pure geofence semantics:
                # - ready: has a polygon (we can geofence work into this region)
                # - blocked: no polygon at all (cannot geofence)
                # "partial" is reserved for future nuance (e.g. metadata gaps),
                # but is not used in this exporter today.
                has_poly = bool(sg.has_subgraph_poly)

                if has_poly:
                    status = "ready"
                else:
                    status = "blocked"

                rows.append(
                    {
                        "iso2": iso2,
                        "iso3": iso3,
                        "canonical_name": profile.canonical_name,
                        "canonical_slug": profile.canonical_slug,
                        "subgraph_name": sg.name,
                        "subgraph_slug": sg.slug,
                        "has_subgraph_pbf": bool(sg.has_subgraph_pbf),
                        "has_subgraph_poly": bool(sg.has_subgraph_poly),
                        "has_subgraph_pickle": bool(sg.has_subgraph_pickle),
                        "subgraph_pbf_path": sg.subgraph_pbf_path or "",
                        "subgraph_poly_path": sg.subgraph_poly_path or "",
                        "subgraph_pickle_path": sg.subgraph_pickle_path or "",
                        "node_count": sg.node_count or 0,
                        "way_count": sg.way_count or 0,
                        "relation_count": sg.relation_count or 0,
                        "file_size_bytes": sg.file_size_bytes or 0,
                        "metadata_status": sg.metadata_status,
                        "status": status,
                    }
                )

        if not rows:
            self.stdout.write(self.style.WARNING("No subgraph rows found to export."))
            return

        fieldnames = [
            "iso2",
            "iso3",
            "canonical_name",
            "canonical_slug",
            "subgraph_name",
            "subgraph_slug",
            "has_subgraph_pbf",
            "has_subgraph_poly",
            "has_subgraph_pickle",
            "subgraph_pbf_path",
            "subgraph_poly_path",
            "subgraph_pickle_path",
            "node_count",
            "way_count",
            "relation_count",
            "file_size_bytes",
            "metadata_status",
            "status",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(rows)} subgraph rows to {output_path}")
        )
