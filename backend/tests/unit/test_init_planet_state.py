"""init_planet run state: honest finalize + step-level resume.

Regression cover for ``ISSUE-init-reliability``.  The old ``_finalize`` stamped
COMPLETED unconditionally, so a chain that failed at ``train_mapqa_parser``
looked finished forever and the entrypoint's "COMPLETED ever" pre-check skipped
init on every later boot.
"""

import pytest

from core.management.commands.init_planet import Command
from core.models import PlanetSnapshot

pytestmark = pytest.mark.django_db(transaction=False, databases=["default"])


def _command(failed_steps=None):
    command = Command()
    command._planet_pbf = None
    command._failed_steps = list(failed_steps or [])
    command._run = None
    return command


@pytest.fixture
def snapshot():
    snap = PlanetSnapshot.objects.create(
        snapshot_date_str="1999_01_01",
        snapshot_date="1999-01-01",
        planet_osm_path="/tmp/test-planet.osh.pbf",
        status=PlanetSnapshot.SnapshotStatus.RUNNING,
    )
    yield snap
    snap.delete()


def test_finalize_marks_completed_when_no_step_failed(snapshot):
    command = _command()
    command._run = snapshot

    command._finalize()

    snapshot.refresh_from_db()
    assert snapshot.status == PlanetSnapshot.SnapshotStatus.COMPLETED
    assert snapshot.completed_at is not None
    assert snapshot.failed_steps == []


def test_finalize_marks_failed_and_raises(snapshot):
    command = _command(failed_steps=["train_mapqa_parser"])
    command._run = snapshot

    with pytest.raises(RuntimeError, match="NOT marking COMPLETED"):
        command._finalize()

    snapshot.refresh_from_db()
    assert snapshot.status == PlanetSnapshot.SnapshotStatus.FAILED
    assert snapshot.completed_at is None
    assert snapshot.failed_steps == ["train_mapqa_parser"]


def test_failed_run_is_not_locked_out(snapshot):
    """A FAILED snapshot must not short-circuit the next boot."""
    snapshot.status = PlanetSnapshot.SnapshotStatus.FAILED
    snapshot.save(update_fields=["status"])

    command = _command()
    command._no_lock = False
    command._only_step = None

    # _acquire_lock only short-circuits on COMPLETED.
    assert command._acquire_lock() is True


def test_persist_run_state_records_outcomes(snapshot):
    command = _command(failed_steps=["sync_hierarchy"])
    command._run = snapshot

    command._persist_run_state(
        {"register_planet": "ok", "sync_hierarchy": "failed"}
    )

    snapshot.refresh_from_db()
    assert snapshot.step_states == {
        "register_planet": "ok",
        "sync_hierarchy": "failed",
    }
    assert snapshot.failed_steps == ["sync_hierarchy"]
