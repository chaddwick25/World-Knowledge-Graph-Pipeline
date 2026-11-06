"""Verify that a planet-init build actually produced a usable substrate.

Read-only by design: it never mutates, so it is safe to run against any
database, including the live pair.

Deliberately **not** wired into the container entrypoint.  The entrypoint's
question is "is this process ready to serve?"; this one is "is this build
complete and correct?", with an exit code as the verdict.  Gates that run on
every boot get ignored; gates that block startup turn a data problem into an
outage.

Usage::

    python manage.py verify_planet_init              # human readable
    python manage.py verify_planet_init --json       # for CI
    python manage.py verify_planet_init --skip-filesystem   # DB-only

Exit code 0 when every check passes, 1 otherwise.
"""

import json
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import PbfFile, PlanetSnapshot, RegionHierarchy
from core.services.snapshot.osmium_facade import OsmiumFacade

# What a complete build looks like.  Overridable because a partial build
# (--skip-continents, or a subset of continents) is a legitimate state.
EXPECTED_CONTINENTS = 9
EXPECTED_AMENITY_ROWS = 626
MIN_REGION_HIERARCHY = 544


class Check:
    """One assertion with a verdict and a human-readable detail line."""

    def __init__(self, name, ok, detail, warn=False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.warn = warn

    def as_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "warn": self.warn}


class Command(BaseCommand):
    help = "Verify a planet-init build (read-only; exit 1 on any failure)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json", action="store_true",
            help="Emit machine-readable JSON instead of text.",
        )
        parser.add_argument(
            "--skip-filesystem", action="store_true",
            help="Skip checks that need the data tree mounted (DB-only run).",
        )
        parser.add_argument(
            "--expect-continents", type=int, default=EXPECTED_CONTINENTS,
            help=f"Expected continent count (default {EXPECTED_CONTINENTS}).",
        )

    # ── checks ────────────────────────────────────────────────────────────

    def _check_pbf_files(self, expect_continents):
        continents = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.CONTINENT
        ).count()
        planets = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.PLANET
        ).count()
        ok = planets >= 1 and continents == expect_continents
        return Check(
            "pbf_files",
            ok,
            f"planet={planets} (need ≥1), continent={continents} "
            f"(need {expect_continents})",
        )

    def _check_continent_profiles(self, expect_continents):
        """The order/poly check: prebuild scans the continent directory."""
        from core.models import ContinentProfile

        actual = ContinentProfile.objects.count()
        ok = actual == expect_continents
        detail = f"expected {expect_continents}, actual {actual}"
        if not ok:
            detail += (
                " — 0 usually means sync_hierarchy ran before the continents "
                "existed (or the poly directory was empty)"
            )
        return Check("continent_profiles", ok, detail)

    def _check_region_hierarchy(self):
        actual = RegionHierarchy.objects.count()
        return Check(
            "region_hierarchy",
            actual >= MIN_REGION_HIERARCHY,
            f"expected ≥{MIN_REGION_HIERARCHY}, actual {actual}",
        )

    def _check_osm_boundaries(self):
        from core.models import OsmBoundary

        actual = OsmBoundary.objects.count()
        return Check(
            "osm_boundaries", actual > 0, f"expected >0, actual {actual}"
        )

    def _check_amenity_rows(self):
        from worldkg_nca.models import AmenityEmbedding

        actual = AmenityEmbedding.objects.using("vectors").count()
        return Check(
            "factor_amenity_embedding",
            actual >= EXPECTED_AMENITY_ROWS,
            f"expected ≥{EXPECTED_AMENITY_ROWS}, actual {actual}",
        )

    def _check_planet_snapshot(self):
        snap = PlanetSnapshot.objects.order_by("-created_at").first()
        if snap is None:
            return Check("planet_snapshot", False, "no PlanetSnapshot rows")
        completed = snap.status == PlanetSnapshot.SnapshotStatus.COMPLETED
        detail = f"{snap.snapshot_date_str} status={snap.status}"
        if snap.failed_steps:
            detail += f" failed_steps={snap.failed_steps}"
        return Check("planet_snapshot", completed and not snap.failed_steps,
                     detail)

    def _check_step_states_vs_reality(self, expect_continents):
        """Cross-check the lock's claim against the counts.

        The failure mode this catches: finalize says COMPLETED while a step
        silently produced nothing (observed twice — Step 5 writing 0 rows,
        and continent_profiles at 0 after a reordered run).
        """
        from core.models import ContinentProfile

        snap = PlanetSnapshot.objects.order_by("-created_at").first()
        if snap is None or not snap.step_states:
            return Check("step_states_consistency", True,
                         "no step state recorded (pre-2026-11 build)", warn=True)
        states = snap.step_states or {}
        problems = []
        if states.get("extract_continents") == "ok" and \
                ContinentProfile.objects.count() != expect_continents:
            problems.append("extract_continents ok but continent_profiles "
                            f"= {ContinentProfile.objects.count()}")
        if states.get("sync_hierarchy") == "ok" and \
                RegionHierarchy.objects.count() < MIN_REGION_HIERARCHY:
            problems.append("sync_hierarchy ok but region_hierarchy is short")
        return Check("step_states_consistency", not problems,
                     "; ".join(problems) if problems else "consistent")

    def _check_continent_headers(self):
        """Every continent must carry history, and the naming should say so."""
        facade = OsmiumFacade()
        missing, no_history, legacy = [], [], []
        for pbf in PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.CONTINENT
        ):
            path = Path(pbf.path)
            if not path.exists():
                missing.append(path.name)
                continue
            result = facade.file_info(str(path))
            header = (result.get("info") or {}).get("header") or {}
            if not header.get("with_history"):
                if path.name.endswith(".osh.pbf"):
                    no_history.append(path.name)
                else:
                    legacy.append(path.name)

        problems = []
        if missing:
            problems.append(f"missing files: {', '.join(missing[:3])}")
        if no_history:
            problems.append(
                f"{len(no_history)} .osh.pbf file(s) report no history"
            )
        detail = "; ".join(problems) if problems else "all continents carry history"
        if legacy and not problems:
            detail += (
                f" ({len(legacy)} legacy .pbf name(s): content is history, "
                "header says otherwise)"
            )
        return Check("continent_headers", not problems, detail,
                     warn=bool(legacy and not problems))

    def _check_mapqa_artifacts(self):
        directory = Path(settings.MAPQA_PARSER_DATA_DIR or "") / "artifacts"
        if not directory.is_dir():
            return Check("mapqa_artifacts", False, f"missing {directory}")
        files = [p for p in directory.iterdir() if p.is_file()]
        return Check("mapqa_artifacts", bool(files),
                     f"{len(files)} file(s) in {directory}")

    # ── runner ────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        expect = options["expect_continents"]
        checks = self._run_checks(expect, options["skip_filesystem"])

        failures = [c for c in checks if not c.ok and not c.warn]

        if options["json"]:
            self.stdout.write(json.dumps(
                {
                    "ok": not failures,
                    "checks": [c.as_dict() for c in checks],
                },
                indent=2,
            ))
        else:
            for check in checks:
                mark = "ok  " if check.ok else ("warn" if check.warn else "FAIL")
                self.stdout.write(f"[{mark}] {check.name:24} {check.detail}")
            if failures:
                self.stderr.write(self.style.ERROR(
                    f"verify_planet_init: {len(failures)} check(s) failed"
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    "verify_planet_init: all checks passed"
                ))

        if failures:
            raise SystemExit(1)

    def _run_checks(self, expect, skip_filesystem):
        """Run each check defensively: a broken check is a failed check.

        A stale schema (a database built before a model migration) otherwise
        surfaces as a traceback from whichever check touched the new column
        first, which reads like a bug in this command rather than "run
        migrate".
        """
        specs = [
            ("pbf_files", lambda: self._check_pbf_files(expect)),
            ("continent_profiles", lambda: self._check_continent_profiles(expect)),
            ("region_hierarchy", self._check_region_hierarchy),
            ("osm_boundaries", self._check_osm_boundaries),
            ("factor_amenity_embedding", self._check_amenity_rows),
            ("planet_snapshot", self._check_planet_snapshot),
            ("step_states_consistency",
             lambda: self._check_step_states_vs_reality(expect)),
        ]
        if not skip_filesystem:
            specs.append(("continent_headers", self._check_continent_headers))
            specs.append(("mapqa_artifacts", self._check_mapqa_artifacts))

        checks = []
        for name, run in specs:
            try:
                checks.append(run())
            except Exception as exc:  # noqa: BLE001 — a broken check is a failure
                detail = str(exc).strip().splitlines()[0]
                if "does not exist" in detail:
                    detail += (
                        " — schema is behind; run `manage.py migrate` against "
                        "this database first"
                    )
                checks.append(Check(name, False, detail))
        return checks
