"""verify_planet_init: the checks that turn a build into a verdict.

Focus is on the checks that catch the failures this project has actually hit:
a reordered init leaving ``continent_profiles`` at 0, a chain that seals
COMPLETED while a step silently produced nothing, and continents whose header
disagrees with their content.
"""

import io
import json

import pytest
from django.core.management import call_command

from core.management.commands.verify_planet_init import Command
from core.models import PbfFile, PlanetSnapshot

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)


def _command():
    return Command(stdout=io.StringIO(), stderr=io.StringIO())


# ── pbf_files ─────────────────────────────────────────────────────────────

def test_pbf_files_fails_on_empty_database():
    check = _command()._check_pbf_files(9)
    assert not check.ok
    assert "planet=0" in check.detail


def test_pbf_files_passes_with_one_planet_and_n_continents():
    PbfFile.objects.create(
        pbf_file_type=PbfFile.PbfType.PLANET, path="/tmp/planet.osh.pbf",
    )
    for index in range(3):
        PbfFile.objects.create(
            pbf_file_type=PbfFile.PbfType.CONTINENT,
            path=f"/tmp/continent_{index}.osh.pbf",
        )

    assert _command()._check_pbf_files(3).ok


# ── continent_profiles: the order/poly check ──────────────────────────────

def test_continent_profiles_zero_names_the_glob_cause():
    """0 means the continent directory scan found nothing, not an ordering bug."""
    check = _command()._check_continent_profiles(9)
    assert not check.ok
    assert "globs the continents directory" in check.detail


# ── planet snapshot + step-state cross-check ──────────────────────────────

def test_planet_snapshot_requires_completed_and_no_failed_steps():
    PlanetSnapshot.objects.create(
        snapshot_date_str="1999_01_02",
        snapshot_date="1999-01-02",
        planet_osm_path="/tmp/p.osh.pbf",
        status=PlanetSnapshot.SnapshotStatus.FAILED,
        failed_steps=["train_mapqa_parser"],
    )

    check = _command()._check_planet_snapshot()
    assert not check.ok
    assert "train_mapqa_parser" in check.detail


def test_step_states_inconsistency_is_caught():
    """finalize said ok, but the continent profiles were never written."""
    PlanetSnapshot.objects.create(
        snapshot_date_str="1999_01_03",
        snapshot_date="1999-01-03",
        planet_osm_path="/tmp/q.osh.pbf",
        status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        step_states={"extract_continents": "ok", "sync_hierarchy": "ok"},
    )

    check = _command()._check_step_states_vs_reality(9)
    assert not check.ok
    assert "continent_profiles" in check.detail


# ── exit code ─────────────────────────────────────────────────────────────

def test_command_exits_nonzero_when_a_check_fails():
    out = io.StringIO()
    with pytest.raises(SystemExit) as exit_info:
        call_command("verify_planet_init", "--skip-filesystem", "--json",
                     stdout=out)

    assert exit_info.value.code == 1
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert any(not check["ok"] for check in payload["checks"])
