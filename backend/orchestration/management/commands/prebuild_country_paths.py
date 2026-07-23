"""
Management command: prebuild_country_paths

Resolves and stores all filesystem paths for every CountryPipelineProfile
and SubgraphProfile. This creates the single ground truth for path resolution --
no more runtime calls to regional_path_service or normalize_country_slug().

The embedding TSVs are the allowlist:
    EMBEDDINGS_ROOT / {continent} / {continent}-{subregion}-location / {slug}-location.tsv.gz
    EMBEDDINGS_ROOT / {continent} / {continent}-{subregion}-tags / {slug}-tags.tsv.gz

This command:
  1. Scans every TSV under EMBEDDINGS_ROOT to build a {slug} -> {location_tsv, tags_tsv} index
  2. Matches each CountryPipelineProfile by canonical_slug (or override slug)
  3. Stores the resolved paths; countries without TSVs get null paths and are skipped by pipeline
  4. OSM_WIKIDATA_EXTRACTIONS_DIR paths are resolved on top of that

Workflow:
  1. First run resolves whatever it can
  2. Review --report for countries that have TSVs but were not matched (need overrides)
  3. Add entries to overrides.json for slug mismatches, re-run

Run once after prebuild_worldkg_structure. Re-runnable (idempotent).
"""

import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models

from extraction.services.regional_path_service import (
    RegionalPathService,
    normalize_country_slug,
    normalize_continent_slug,
)
from extraction.services.country_override_service import (
    get_country_slug,
    get_country_override_record,
    get_embedding_edge_case,
    load_overrides,
)


class Command(BaseCommand):
    help = "Pre-compute and store all resolved filesystem paths for countries and subgraphs."
    # TODO: remove the hard coded dates
    SNAPSHOT_DATE = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
    def add_arguments(self, parser):
        parser.add_argument(
            '--iso',
            default=None,
            help='Process only a single ISO code (e.g., IE)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without writing to DB',
        )
        parser.add_argument(
            '--snapshot-date',
            default=self.SNAPSHOT_DATE,
            help=f'Snapshot date (default: {self.SNAPSHOT_DATE})',
        )
        parser.add_argument(
            '--report',
            action='store_true',
            help='Report mode: show TSV index coverage and matches (no writes)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        single_iso = (options.get('iso') or '').upper() if options.get('iso') else None
        snapshot_date = options['snapshot_date']
        report_only = options.get('report', False)
        self.rps = RegionalPathService()

        from orchestration.models import (
            CountryPipelineProfile,
            SubgraphProfile,
        )

        embedding_root = self._resolve_embedding_root()
        if not embedding_root:
            self.stdout.write(self.style.ERROR("EMBEDDINGS_ROOT not configured or does not exist."))
            return

        # -- Build TSV index from disk -----------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nScanning TSVs under {embedding_root}..."
        ))
        tsv_index = self._build_tsv_index(embedding_root)
        self.stdout.write(f"  Indexed {len(tsv_index)} unique slugs with TSV files\n")

        if report_only:
            self._print_report(tsv_index, single_iso)
            return

        # -- Resolve paths for each profile -------------------------------
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("Country Paths Pre-Build"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        profiles = CountryPipelineProfile.objects.all()
        if single_iso:
            profiles = profiles.filter(
                models.Q(iso2__iexact=single_iso) | models.Q(iso3__iexact=single_iso)
            )

        matched = []
        unmatched = []

        for profile in profiles:
            result = self._resolve_country_paths(
                profile, tsv_index, snapshot_date, dry_run
            )

            has_tsvs = (
                result.get("embedding_location_tsv_path")
                or result.get("embedding_tags_tsv_path")
            )

            if has_tsvs:
                matched.append((profile, result))
                self.stdout.write(
                    f"  {profile.canonical_name} "
                    f"({profile.iso3 or profile.iso2 or '?'})"
                )
            else:
                unmatched.append((profile, result))
                self.stdout.write(
                    self.style.WARNING(
                        f"  - {profile.canonical_name}: no TSV match "
                        f"(slug={result.get('slug')})"
                    )
                )

        # Subgraph paths
        subgraph_qs = SubgraphProfile.objects.filter(has_subgraph_pbf=True)
        if single_iso:
            subgraph_qs = subgraph_qs.filter(
                country_profile__in=CountryPipelineProfile.objects.filter(
                    models.Q(iso2__iexact=single_iso) | models.Q(iso3__iexact=single_iso)
                )
            )

        sg_ok = 0
        for sg in subgraph_qs:
            try:
                self._resolve_subgraph_paths(sg, snapshot_date, dry_run)
                sg_ok += 1
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f"  X {sg.country_profile.canonical_name}/{sg.name}: {exc}")
                )

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(
            self.style.SUCCESS(
                f"Complete: {len(matched)} countries with TSVs, "
                f"{len(unmatched)} without TSVs; "
                f"{sg_ok} subgraphs"
            )
        )

        if unmatched:
            self.stdout.write(self.style.WARNING(
                "\nCountries without TSV matches -- they won't run in the pipeline."
            ))
            for p, r in sorted(unmatched, key=lambda x: x[0].canonical_name):
                iso = p.iso3 or p.iso2 or "?"
                self.stdout.write(f"    {p.canonical_name} ({iso}) slug={r.get('slug')}")

    # ======================================================================
    # TSV Index
    # ======================================================================

    def _build_tsv_index(self, embedding_root: Path) -> Dict[str, dict]:
        """Scan EMBEDDINGS_ROOT recursively and build {slug: {location_tsv, tags_tsv}}.

        Each TSV file is named {slug}-{type}.tsv.gz where type is 'location' or 'tags'.
        The file lives under {continent}/{subdir}/ where subdir might be:
          - {continent}-{subregion}-location/  (e.g., europe-west-location)
          - {continent}-location/              (e.g., africa-location)
          - {country_slug}-location/           (e.g., france-location)

        Returns:
            Dict mapping slug -> {
                "location_tsv": str or None,
                "tags_tsv": str or None,
                "continent": str,
                "subregion": str,
            }
        """
        index = {}

        for tsv_path in embedding_root.rglob("*.tsv.gz"):
            # Parse filename: "ireland-and-northern-ireland-location.tsv.gz"
            stem = tsv_path.stem.replace(".tsv", "")  # removes .gz then .tsv
            # Split on last hyphen to get type
            parts = stem.rsplit("-", 1)
            if len(parts) != 2:
                continue
            slug_part, tsv_type = parts
            tsv_type = tsv_type.lower()

            if tsv_type not in ("location", "tags"):
                continue

            # Determine continent and subregion from parent dir
            parent_dir = tsv_path.parent.name  # e.g. "europe-west-location"
            continent_dir = tsv_path.parent.parent.name  # e.g. "europe"

            entry = index.setdefault(slug_part, {
                "location_tsv": None,
                "tags_tsv": None,
                "continent": continent_dir,
                "subregion": parent_dir.replace(f"-{tsv_type}", ""),
            })

            entry[tsv_type + "_tsv"] = str(tsv_path)
            entry["continent"] = continent_dir
            entry["subregion"] = parent_dir.replace(f"-{tsv_type}", "")

        return index

    # ======================================================================
    # Country path resolution
    # ======================================================================

    def _resolve_country_paths(
        self,
        profile: "CountryPipelineProfile",
        tsv_index: Dict[str, dict],
        snapshot_date: str,
        dry_run: bool,
    ) -> dict:
        """
        Resolve all paths for a country profile.

        Uses the TSV index to find embedding files, then resolves OSM paths.
        """
        iso = profile.iso3 or profile.iso2 or ""
        default_slug = profile.canonical_slug

        # IMPORTANT: candidate_slugs prioritises canonical_slug first.
        # TSV filenames use the Geofabrik-derived slug (hyphens),
        # which matches canonical_slug. The overrides.json 'country_slug'
        # is for OSM path resolution, NOT for TSV matching.
        override = get_country_override_record(iso)
        override_country_slug = override.get("country_slug") if override else None
        override_emb_slug = override.get("embedding_slug") if override else None

        # Build candidate list in priority order:
        # 1. override.embedding_slug (explicit TSV filename override)
        # 2. canonical_slug (the Geofabrik slug, matches most TSV filenames)
        # 3. override.country_slug (for OSM paths, fallback for TSV)
        # 4. normalized canonical name (underscores -> hyphens)
        # 5. iso lowercase
        candidate_slugs = []
        if override_emb_slug:
            candidate_slugs.append(override_emb_slug)
        candidate_slugs.append(default_slug)  # canonical_slug matches TSV filenames
        if override_country_slug and override_country_slug not in candidate_slugs:
            candidate_slugs.append(override_country_slug)
        name_slug = normalize_country_slug(profile.canonical_name)
        if name_slug not in candidate_slugs:
            candidate_slugs.append(name_slug)
        if iso and iso.lower() not in candidate_slugs:
            candidate_slugs.append(iso.lower())

        continent_name = profile.continent_name or ""
        cont_slug = normalize_continent_slug(continent_name)

        loc_tsv = None
        tags_tsv = None
        matched_slug = None
        for cs in candidate_slugs:
            entry = tsv_index.get(cs)
            if entry:
                loc_tsv = entry.get("location_tsv")
                tags_tsv = entry.get("tags_tsv")
                matched_slug = cs
                # Also enrich continent if not set
                if not cont_slug and entry.get("continent"):
                    cont_slug = entry["continent"]
                break

        # -- embedding_edge_cases: shared location/tag overrides -----------
        # Some countries borrow location or tag TSVs from another shard.
        # For example, Poland borrows europe-east location but keeps its own
        # tags. Germany has split node/way location and tag views.
        # See data/overrides.json → embedding_edge_cases for the full config.
        if matched_slug:
            edge_case = get_embedding_edge_case(matched_slug)
            if edge_case:
                # Shared location overrides: use source shard's location TSV
                for slo in edge_case.get("shared_location_overrides", []):
                    source_slug = slo.get("source")
                    targets = slo.get("targets", [])
                    if matched_slug in targets and source_slug:
                        source_entry = tsv_index.get(source_slug)
                        if source_entry and source_entry.get("location_tsv"):
                            loc_tsv = source_entry["location_tsv"]
                            self.stdout.write(
                                self.style.WARNING(
                                    f"    [edge_case] {matched_slug}: using location TSV "
                                    f"from '{source_slug}' ({slo.get('note', '')})"
                                )
                            )

                # Shared tag overrides: use source shard's tags TSV
                for sto in edge_case.get("shared_tag_overrides", []):
                    source_slug = sto.get("source")
                    targets = sto.get("targets", [])
                    if matched_slug in targets and source_slug:
                        source_entry = tsv_index.get(source_slug)
                        if source_entry and source_entry.get("tags_tsv"):
                            tags_tsv = source_entry["tags_tsv"]
                            self.stdout.write(
                                self.style.WARNING(
                                    f"    [edge_case] {matched_slug}: using tags TSV "
                                    f"from '{source_slug}' ({sto.get('note', '')})"
                                )
                            )

        # -- OSM_WIKIDATA_EXTRACTIONS_DIR paths ---------------------------
        # For OSM paths, use the override country_slug (which maps to Geofabrik dir names)
        osm_slug = override_country_slug or matched_slug or default_slug
        osm_dir = getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', None)
        if osm_dir:
            country_osm_dir = Path(osm_dir) / cont_slug / osm_slug if cont_slug else None
        else:
            country_osm_dir = None

        snap_pbf = None
        snap_poly = None
        if country_osm_dir:
            snap_dir = country_osm_dir / "temporal_snapshots"
            snap_pbf_candidate = snap_dir / f"{osm_slug}_{snapshot_date}.osm.pbf"
            if snap_pbf_candidate.exists():
                snap_pbf = str(snap_pbf_candidate)
            snap_poly_candidate = snap_dir / f"{osm_slug}_{snapshot_date}.osm.poly"
            if snap_poly_candidate.exists():
                snap_poly = str(snap_poly_candidate)

        pickle_dir = None
        if country_osm_dir:
            pickle_dir = str(country_osm_dir / "pickle")

        # Continent PBF
        cont_pbf = None
        if osm_dir and cont_slug:
            cont_pbf_path = Path(osm_dir) / "continents" / f"{cont_slug}.pbf"
            if not cont_pbf_path.exists():
                cont_pbf_path = Path(osm_dir) / "continents" / f"{cont_slug}.osm.pbf"
            if cont_pbf_path.exists():
                cont_pbf = str(cont_pbf_path)

        result = {
            "iso": iso,
            "slug": matched_slug or osm_slug or default_slug,
            "matched_tsv_slug": matched_slug,
            "cont_slug": cont_slug,
            "embedding_location_tsv_path": loc_tsv,
            "embedding_tags_tsv_path": tags_tsv,
            "snapshot_pbf_path": snap_pbf,
            "snapshot_poly_path": snap_poly,
            "pickle_dir": pickle_dir,
            "continent_pbf_path": cont_pbf,
        }

        if dry_run:
            for k, v in result.items():
                if v:
                    self.stdout.write(f"    {k}: {v}")
                else:
                    self.stdout.write(self.style.WARNING(f"    {k}: (not found)"))
            return result

        # -- Write to DB --------------------------------------------------
        effective_override_slug = override.get("country_slug") if override else None
        effective_override_poly_slug = override.get("poly_slug") if override else None
        effective_override_emb_slug_for_field = override.get("embedding_slug") if override else None

        profile.embedding_location_tsv_path = result["embedding_location_tsv_path"]
        profile.embedding_tags_tsv_path = result["embedding_tags_tsv_path"]
        profile.snapshot_pbf_path = result["snapshot_pbf_path"]
        profile.snapshot_poly_path = result["snapshot_poly_path"]
        profile.pickle_dir = result["pickle_dir"]
        profile.continent_pbf_path = result["continent_pbf_path"]

        profile.override_country_slug = effective_override_slug
        profile.override_poly_slug = effective_override_poly_slug
        profile.override_embedding_slug = effective_override_emb_slug_for_field
        profile.has_override = effective_override_slug is not None
        profile.overrides_synced_at = __import__(
            'django.utils.timezone', fromlist=['timezone']
        ).now()

        profile.save(update_fields=[
            "embedding_location_tsv_path",
            "embedding_tags_tsv_path",
            "snapshot_pbf_path",
            "snapshot_poly_path",
            "pickle_dir",
            "continent_pbf_path",
            "override_country_slug",
            "override_poly_slug",
            "override_embedding_slug",
            "has_override",
            "overrides_synced_at",
            "updated_at",
        ])

        return result

    # ======================================================================
    # Subgraph path resolution
    # ======================================================================

    def _resolve_subgraph_paths(
        self,
        sg: "SubgraphProfile",
        snapshot_date: str,
        dry_run: bool,
    ) -> None:
        """Resolve subgraph PBF/poly/pickle paths and store on the model."""
        profile = sg.country_profile

        iso = profile.iso3 or profile.iso2 or ""
        default_slug = profile.canonical_slug
        country_slug = get_country_slug(iso, default_slug)
        continent_name = profile.continent_name or ""
        sg_slug = sg.slug or normalize_country_slug(sg.name)
        cont_slug = normalize_continent_slug(continent_name)

        osm_dir = getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', None)
        if not osm_dir:
            return

        sg_dir = Path(osm_dir) / cont_slug / country_slug / "subgraphs" / sg_slug

        sg_pbf = sg_dir / f"{sg_slug}_{snapshot_date}.osm.pbf"
        sg_pbf = str(sg_pbf) if sg_pbf.exists() else sg.subgraph_pbf_path

        sg_poly = sg_dir / f"{sg_slug}_{snapshot_date}.osm.poly"
        sg_poly = str(sg_poly) if sg_poly.exists() else sg.subgraph_poly_path

        sg_pickle = Path(osm_dir) / cont_slug / country_slug / "pickles" / sg_slug / "wdw.pickle"
        sg_pickle = str(sg_pickle) if sg_pickle.exists() else sg.subgraph_pickle_path

        if dry_run:
            self.stdout.write(f"    {sg.name}:")
            self.stdout.write(f"      subgraph_pbf_path: {sg_pbf}")
            self.stdout.write(f"      subgraph_poly_path: {sg_poly}")
            self.stdout.write(f"      subgraph_pickle_path: {sg_pickle}")
            return

        sg.subgraph_pbf_path = sg_pbf
        sg.subgraph_poly_path = sg_poly
        sg.subgraph_pickle_path = sg_pickle

        sg.has_subgraph_pbf = sg_pbf is not None
        sg.has_subgraph_poly = sg_poly is not None
        sg.has_subgraph_pickle = sg_pickle is not None

        sg.save(update_fields=[
            "subgraph_pbf_path",
            "subgraph_poly_path",
            "subgraph_pickle_path",
            "has_subgraph_pbf",
            "has_subgraph_poly",
            "has_subgraph_pickle",
            "updated_at",
        ])

    # ======================================================================
    # Report mode
    # ======================================================================

    def _print_report(self, tsv_index: Dict[str, dict], single_iso: str = None):
        """Print the current state of TSV coverage and profile matches."""
        from orchestration.models import CountryPipelineProfile

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("TSV Index Report"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        # Group TSVs by continent
        by_continent = defaultdict(list)
        for slug, entry in sorted(tsv_index.items()):
            by_continent[entry["continent"]].append((slug, entry))

        for continent in sorted(by_continent):
            entries = by_continent[continent]
            has_loc = sum(1 for _, e in entries if e["location_tsv"])
            has_tags = sum(1 for _, e in entries if e["tags_tsv"])
            self.stdout.write(f"\n  {continent}/: {len(entries)} slugs, "
                              f"{has_loc} with location TSV, {has_tags} with tags TSV")

            # Show subregions
            subregions = defaultdict(list)
            for slug, entry in entries:
                subregions[entry["subregion"]].append(slug)
            for subregion, slugs in sorted(subregions.items()):
                self.stdout.write(f"    {subregion}: {len(slugs)} slugs")

        # Match against profiles
        self.stdout.write(self.style.MIGRATE_HEADING("\nProfile Matching"))

        profiles = CountryPipelineProfile.objects.all()
        if single_iso:
            profiles = profiles.filter(
                models.Q(iso2__iexact=single_iso) | models.Q(iso3__iexact=single_iso)
            )

        matched = []
        tsv_without_profile = set(tsv_index.keys())
        profile_without_tsv = []

        for p in profiles:
            iso = p.iso3 or p.iso2 or ""
            default_slug = p.canonical_slug

            override = get_country_override_record(iso)
            override_country_slug = override.get("country_slug") if override else None
            override_emb_slug = override.get("embedding_slug") if override else None

            candidates = []
            if override_emb_slug:
                candidates.append(override_emb_slug)
            candidates.append(default_slug)
            if override_country_slug and override_country_slug not in candidates:
                candidates.append(override_country_slug)
            name_slug = normalize_country_slug(p.canonical_name)
            if name_slug not in candidates:
                candidates.append(name_slug)
            if iso and iso.lower() not in candidates:
                candidates.append(iso.lower())

            found = None
            for cs in candidates:
                if cs in tsv_index:
                    found = cs
                    break

            if found:
                matched.append((p, found))
                tsv_without_profile.discard(found)
            else:
                profile_without_tsv.append((p, candidates))

        self.stdout.write(f"\n  {len(matched)} profiles matched to TSVs")
        for p, slug in sorted(matched, key=lambda x: x[0].canonical_name):
            iso = p.iso3 or p.iso2 or "?"
            entry = tsv_index[slug]
            hint = f" [{entry['continent']}/{entry['subregion']}]" if entry.get("subregion") else ""
            self.stdout.write(f"    {p.canonical_name} ({iso}) -> {slug}{hint}")

        if profile_without_tsv:
            self.stdout.write(self.style.WARNING(
                f"\n  {len(profile_without_tsv)} profiles with NO TSV match "
                "(won't run pipeline):"
            ))
            for p, candidates in sorted(profile_without_tsv, key=lambda x: x[0].canonical_name):
                iso = p.iso3 or p.iso2 or "?"
                self.stdout.write(f"    {p.canonical_name} ({iso}) tried: {candidates}")

        if tsv_without_profile:
            self.stdout.write(self.style.WARNING(
                f"\n  {len(tsv_without_profile)} TSV slugs with NO profile "
                "(run sync_country_pipeline_profiles first?):"
            ))
            for slug in sorted(tsv_without_profile):
                entry = tsv_index[slug]
                self.stdout.write(f"    {slug} [{entry['continent']}/{entry['subregion']}]")

        # Quick overrides template for slug mismatches
        mismatches = [
            (p, candidates) for p, candidates in profile_without_tsv
            if any(c in tsv_index for c in candidates[1:])
        ]
        if mismatches:
            self.stdout.write(self.style.WARNING(
                "\n  These profiles have a TSV match via fallback slug but not via canonical_slug."
                "\n    Add an override to make the match explicit:"
            ))
            for p, candidates in sorted(mismatches, key=lambda x: x[0].canonical_name):
                iso = p.iso3 or p.iso2 or "??"
                for c in candidates[1:]:
                    if c in tsv_index:
                        self.stdout.write(
                            f'    "{iso}": {{ "embedding_slug": "{c}" }}  '
                            f"# {p.canonical_name} (canonical={p.canonical_slug})"
                        )
                        break

    # ======================================================================
    # Utilities
    # ======================================================================

    def _resolve_embedding_root(self) -> Optional[Path]:
        emb_root = getattr(settings, 'EMBEDDINGS_ROOT', None)
        if emb_root:
            p = Path(emb_root)
            if p.exists():
                return p
        return None
