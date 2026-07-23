import json
import shutil
from pathlib import Path
from typing import List, Optional

from django.core.management.base import BaseCommand
from django.conf import settings

from api.models import ProcessingSession
from extraction.services.hierarchy_cache_service import hierarchy_cache_service
from extraction.services.country_override_service import get_country_slug
from extraction.services.regional_path_service import regional_path_service, normalize_country_slug


class Command(BaseCommand):
    help = (
        "Sync Wikidata-based OSM relation hierarchies for countries "
        "using country_relations.json and cache them under "
        "data/osm_wikidata_hierarchy_cache/."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--continent",
            type=str,
            help="Optional continent filter (e.g. europe, africa).",
        )
        parser.add_argument(
            "--countries",
            type=str,
            help=(
                "Optional comma-separated list of ISO country codes "
                "to restrict processing. If omitted, all countries or "
                "all within --continent are processed."
            ),
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help=(
                "Run batch synchronously (wait for Celery task to complete). "
                "If omitted, the batch is dispatched asynchronously."
            ),
        )

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        json_path = base_dir / "data" / "country_relations.json"

        if not json_path.exists():
            self.stdout.write(
                self.style.ERROR(
                    f"country_relations.json not found at {json_path}. "
                    f"Run 'sync_country_relations' first."
                )
            )
            return

        with open(json_path, "r") as f:
            country_relations = json.load(f)

        continent_filter: Optional[str] = options.get("continent")
        requested_countries: Optional[str] = options.get("countries")

        isos: List[str] = []

        if requested_countries:
            isos = [c.strip().upper() for c in requested_countries.split(",") if c.strip()]
        else:
            # Use all ISOs in country_relations, optionally filtered by continent
            for iso, entry in country_relations.items():
                if not isinstance(entry, dict):
                    continue
                if continent_filter:
                    cont = entry.get("continent_name") or entry.get("parent_slug")
                    if not cont or cont.lower() != continent_filter.lower():
                        continue
                isos.append(iso.upper())

        if not isos:
            self.stdout.write(self.style.WARNING("No countries to process (check filters)."))
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Syncing Wikidata hierarchies for {len(isos)} countries..."
            )
        )

        # Create ProcessingSession for observability
        session = ProcessingSession.objects.create(
            session_name=f"Wikidata Hierarchy Sync ({len(isos)} countries)",
            session_type=ProcessingSession.SessionType.GEOGRAPHIC_EXTRACT,
            configuration={
                "isos": isos,
                "continent_filter": continent_filter,
                "hierarchy_cache_dir": "data/osm_wikidata_hierarchy_cache",
            },
            status=ProcessingSession.SessionStatus.IN_PROGRESS,
        )

        if options.get("sync"):
            # Run synchronously in this process
            results = []
            for iso in isos:
                try:
                    res = hierarchy_cache_service.get_or_build_hierarchy(iso)
                    results.append(res)
                except Exception as exc:
                    results.append({
                        "success": False, "iso": iso, "error": str(exc),
                    })
            summary = {
                "success": True,
                "countries": len(results),
                "success_count": sum(1 for r in results if r.get("success")),
                "failure_count": sum(1 for r in results if not r.get("success")),
                "children_total": sum(r.get("children", 0) for r in results),
                "results": results,
            }
            self._print_summary(summary)

            # Copy hierarchies to subgraphs directory for successful countries
            self._copy_to_subgraphs(isos, country_relations)
        else:
            # For async dispatch, just process synchronously since the underlying
            # HierarchyCacheService builds in-memory without long-running tasks.
            self.stdout.write(
                self.style.WARNING(
                    "Async dispatch not supported (task function missing). "
                    "Running synchronously instead."
                )
            )
            results = []
            for iso in isos:
                try:
                    res = hierarchy_cache_service.get_or_build_hierarchy(iso)
                    results.append(res)
                except Exception as exc:
                    results.append({
                        "success": False, "iso": iso, "error": str(exc),
                    })
            summary = {
                "success": True,
                "countries": len(results),
                "success_count": sum(1 for r in results if r.get("success")),
                "failure_count": sum(1 for r in results if not r.get("success")),
                "children_total": sum(r.get("children", 0) for r in results),
                "results": results,
            }
            self._print_summary(summary)
            self._copy_to_subgraphs(isos, country_relations)

    def _copy_to_subgraphs(self, isos: List[str], country_relations: dict):
        """Copy hierarchy JSON files to subgraphs directory for each country."""
        base_dir = Path(settings.BASE_DIR).parent
        cache_dir = base_dir / "backend" / "data" / "osm_wikidata_hierarchy_cache"

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Copying hierarchies to subgraphs directories..."))

        copied = 0
        skipped = 0
        failed = 0

        for iso in isos:
            iso = iso.upper()
            cache_path = cache_dir / f"{iso}_hierarchy.json"

            if not cache_path.exists():
                self.stdout.write(f"  [SKIP] {iso}: cache file not found")
                skipped += 1
                continue

            # Get country info from country_relations.json (case-insensitive lookup)
            entry = country_relations.get(iso.lower())
            if not entry:
                # Try case-insensitive search
                for k, v in country_relations.items():
                    if k.upper() == iso:
                        entry = v
                        break
            if not entry:
                self.stdout.write(f"  [SKIP] {iso}: not found in country_relations.json")
                skipped += 1
                continue

            continent = entry.get("continent_name") or entry.get("parent_slug")
            country_name = entry.get("name")

            # Derive country_slug using JSON-based ISO overrides where needed
            default_slug = entry.get("slug", normalize_country_slug(country_name))
            country_slug = get_country_slug(iso, default_slug)

            if not continent or not country_slug:
                self.stdout.write(f"  [SKIP] {iso}: missing continent or slug in country_relations")
                skipped += 1
                continue

            try:
                # Resolve subgraphs path
                subgraphs_path = regional_path_service.get_subgraph_hierarchy_path(
                    continent, country_slug, iso
                )

                # Copy file
                shutil.copy2(cache_path, subgraphs_path)
                copied += 1
                self.stdout.write(f"  [OK] {iso}: copied to {subgraphs_path}")
            except Exception as e:
                failed += 1
                self.stdout.write(f"  [FAIL] {iso}: {e}")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Subgraphs copy complete: {copied} copied, {skipped} skipped, {failed} failed."
            )
        )

    def _print_summary(self, summary: dict):
        success = summary.get("success")
        total = summary.get("countries", 0)
        ok = summary.get("success_count", 0)
        failed = summary.get("failure_count", 0)
        children_total = summary.get("children_total", 0)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Hierarchy sync complete: {ok}/{total} succeeded, {failed} failed, "
                f"total children discovered: {children_total}."
            )
        )

        # Optional: print a small table of per-country results
        for res in summary.get("results", []):
            iso = res.get("iso")
            if res.get("success"):
                self.stdout.write(f"  [OK] {iso}: children={res.get('children', 0)}")
            else:
                self.stdout.write(f"  [FAIL] {iso}: error={res.get('error')}")
