"""
Unit tests for the admin job-control plane (ISSUE-admin-job-control-plane).

Covers, with no DB / network / GPU required (ORM surfaces mocked):

  1. Month helpers (parse / iterate / month-end pinning)
  2. Snapshot-date floor validation (``SNAPSHOT_DATE_FLOOR``)
  3. ``dispatch_country_pipeline`` — the shared SnapshotJob gate
  4. ``run_snapshot_backfill`` — validation (floor, ordering, ceiling,
     inter-run GPU exclusion) + per-month SnapshotJob gate
  5. ``UpsertOnlyWriter`` — upserts without the FastText encode
  6. Calendar payload — drift-source jobs never offered as queryable
"""

from unittest.mock import MagicMock, patch

import pytest

from pipeline.backfill import (
    BackfillError,
    iter_months,
    month_end_snapshot_date,
    parse_month,
    run_snapshot_backfill,
)


# =============================================================================
# 1. Month helpers
# =============================================================================

class TestMonthHelpers:

    def test_parse_month_valid(self):
        assert parse_month("2024_03") == (2024, 3)

    def test_parse_month_invalid_format(self):
        with pytest.raises(BackfillError):
            parse_month("2024-03")
        with pytest.raises(BackfillError):
            parse_month("junk")
        with pytest.raises(BackfillError):
            parse_month(None)

    def test_parse_month_out_of_range(self):
        with pytest.raises(BackfillError):
            parse_month("2024_13")

    def test_iter_months_year_boundary(self):
        assert list(iter_months("2024_11", "2025_02")) == [
            "2024_11", "2024_12", "2025_01", "2025_02",
        ]

    def test_iter_months_single(self):
        assert list(iter_months("2023_07", "2023_07")) == ["2023_07"]

    def test_month_end_snapshot_date(self):
        assert month_end_snapshot_date("2024_02") == "2024_02_29"  # leap year
        assert month_end_snapshot_date("2023_02") == "2023_02_28"
        assert month_end_snapshot_date("2025_12") == "2025_12_31"


# =============================================================================
# 2. Snapshot-date floor validation
# =============================================================================

class TestSnapshotDateFloor:

    def _validate(self, snapshot_date):
        from core.services.pipeline.run_dispatch_service import (
            validate_snapshot_date_floor,
        )
        return validate_snapshot_date_floor(snapshot_date)

    def test_bad_format_rejected(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
        )
        with pytest.raises(PipelineDispatchError) as exc:
            self._validate("2020-09-01")
        assert exc.value.code == "invalid"

    def test_before_floor_rejected(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
        )
        with pytest.raises(PipelineDispatchError) as exc:
            self._validate("2020_08_31")
        assert "2020_09" in str(exc.value)

    def test_at_floor_accepted(self):
        self._validate("2020_09_01")  # must not raise

    def test_after_floor_accepted(self):
        self._validate("2025_12_31")  # must not raise


# =============================================================================
# 3. dispatch_country_pipeline — the shared SnapshotJob gate
# =============================================================================

class TestDispatchCountryPipeline:

    def _patch_orm(self, existing_job=None):
        """Patch the SnapshotJob manager + pipeline dispatch entry point."""
        job_manager = MagicMock()
        # Status enum must compare against plain strings on mock jobs
        job_manager.Status = MagicMock(
            RUNNING="running", COMPLETED="completed",
            FAILED="failed", PENDING="pending",
        )
        job_manager.objects.select_for_update().filter.return_value.first \
            .return_value = existing_job
        created_job = MagicMock(id="job-uuid-1")
        job_manager.objects.create.return_value = created_job
        return job_manager, created_job

    def test_missing_country_name_rejected(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
            dispatch_country_pipeline,
        )
        with pytest.raises(PipelineDispatchError) as exc:
            dispatch_country_pipeline("   ")
        assert exc.value.code == "invalid"

    def test_unresolvable_country_rejected(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
            dispatch_country_pipeline,
        )
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value=None,
        ):
            with pytest.raises(PipelineDispatchError) as exc:
                dispatch_country_pipeline("Atlantis")
        assert "Could not resolve ISO" in str(exc.value)

    def test_running_job_conflicts_without_force(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
            dispatch_country_pipeline,
        )
        running_job = MagicMock(status="running")
        job_manager, _ = self._patch_orm(existing_job=running_job)
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.SnapshotJob", job_manager), patch(
            "django.db.transaction.atomic", MagicMock(),
        ):
            with pytest.raises(PipelineDispatchError) as exc:
                dispatch_country_pipeline("Jamaica", snapshot_date="2025_12_31")
        assert exc.value.code == "conflict"
        running_job.delete.assert_not_called()

    def test_failed_job_deleted_and_redispatched(self):
        from core.services.pipeline.run_dispatch_service import (
            dispatch_country_pipeline,
        )
        failed_job = MagicMock(status="failed")
        job_manager, created_job = self._patch_orm(existing_job=failed_job)
        run_manager = MagicMock()
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.SnapshotJob", job_manager), patch(
            "django.db.transaction.atomic", MagicMock(),
        ), patch(
            "pipeline.canvas.run_worldkg_pipeline",
            return_value="run-uuid-1",
        ), patch("core.models.PipelineRun", run_manager):
            result = dispatch_country_pipeline(
                "Jamaica", snapshot_date="2025_12_31",
            )
        failed_job.delete.assert_called_once()
        assert result["pipeline_run_id"] == "run-uuid-1"
        assert result["snapshot_job_id"] == "job-uuid-1"
        # Transitioned to RUNNING after dispatch
        assert created_job.status == "running"

    def test_floor_violation_marks_no_job(self):
        from core.services.pipeline.run_dispatch_service import (
            PipelineDispatchError,
            dispatch_country_pipeline,
        )
        job_manager, _ = self._patch_orm()
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.SnapshotJob", job_manager):
            with pytest.raises(PipelineDispatchError) as exc:
                dispatch_country_pipeline(
                    "Jamaica", snapshot_date="2019_01_01",
                )
        assert exc.value.code == "invalid"
        # Gate never opened — no job created, no dispatch
        job_manager.objects.create.assert_not_called()


# =============================================================================
# 4. run_snapshot_backfill — validation + per-month gate
# =============================================================================

class TestRunSnapshotBackfill:

    def _patch_world(self, snapshots=("2025_12_31",), running_pipeline=False,
                     existing_jobs=None):
        """Patch Snapshot / SnapshotJob / PipelineRun / resolver / chain."""
        snapshot_manager = MagicMock()
        snapshot_manager.objects.filter.return_value.order_by.return_value \
            .values_list.return_value.first.return_value = (
                snapshots[0] if snapshots else None
            )

        job_manager = MagicMock()
        # Status enum must compare against plain strings on mock jobs
        job_manager.Status = MagicMock(
            RUNNING="running", COMPLETED="completed",
            FAILED="failed", PENDING="pending",
        )
        # Default: no existing jobs for any month
        job_manager.objects.filter.return_value.first.return_value = (
            existing_jobs[0] if existing_jobs else None
        )
        created_jobs = [MagicMock(id=f"job-{i}") for i in range(36)]
        job_manager.objects.create.side_effect = created_jobs

        run_manager = MagicMock()
        run_manager.PipelineStatus = MagicMock(RUNNING="RUNNING")
        run = MagicMock(id="backfill-run-1",
                        status=MagicMock(__eq__=lambda self, o: False))
        run_manager.objects.create.return_value = run
        run_manager.objects.filter.return_value.exists.return_value = \
            running_pipeline

        return snapshot_manager, job_manager, run_manager, run

    def test_missing_country_rejected(self):
        with pytest.raises(BackfillError):
            run_snapshot_backfill("", "2024_01", "2024_03")

    def test_start_before_floor_rejected(self):
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ):
            with pytest.raises(BackfillError) as exc:
                run_snapshot_backfill("Jamaica", "2019_12", "2020_02")
        assert "2020_09" in str(exc.value)

    def test_start_after_end_rejected(self):
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ):
            with pytest.raises(BackfillError):
                run_snapshot_backfill("Jamaica", "2024_06", "2024_03")

    def test_no_snapshots_for_country_rejected(self):
        snapshot_manager, job_manager, run_manager, _ = self._patch_world(
            snapshots=(),
        )
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager):
            with pytest.raises(BackfillError) as exc:
                run_snapshot_backfill("Jamaica", "2024_01", "2024_03")
        assert "run the country pipeline first" in str(exc.value)

    def test_end_after_latest_snapshot_rejected(self):
        snapshot_manager, job_manager, run_manager, _ = self._patch_world(
            snapshots=("2024_12_31",),
        )
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager):
            with pytest.raises(BackfillError) as exc:
                run_snapshot_backfill("Jamaica", "2024_01", "2025_06")
        assert "latest" in str(exc.value)

    def test_running_pipeline_blocks_submit(self):
        snapshot_manager, job_manager, run_manager, _ = self._patch_world(
            running_pipeline=True,
        )
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager):
            with pytest.raises(BackfillError) as exc:
                run_snapshot_backfill("Jamaica", "2024_01", "2024_03")
        assert "GPU" in str(exc.value)
        # The freshness window is part of the guard — zombie runs
        # (RUNNING but untouched >24h) must not block.
        _, filter_kwargs = run_manager.objects.filter.call_args
        assert filter_kwargs.get("status") == "RUNNING"
        assert "updated_at__gte" in filter_kwargs

    def test_completed_months_skipped_and_chain_dispatched(self):
        snapshot_manager, job_manager, run_manager, run = self._patch_world()
        # Month 1 completed, months 2-3 open
        completed_job = MagicMock(status="completed")
        job_manager.objects.filter.return_value.first.side_effect = [
            completed_job, None, None,
        ]
        chain_cls = MagicMock()
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager), patch(
            "celery.chain", chain_cls,
        ):
            result = run_snapshot_backfill("Jamaica", "2024_01", "2024_03")
        assert result["dispatched"] is True
        assert result["skipped_months"] == ["2024_01"]
        assert len(result["months"]) == 2
        completed_job.delete.assert_not_called()
        # Two tasks in the chain, dispatched with the run id as task id
        assert chain_cls.return_value.apply_async.call_count == 1
        _, kwargs = chain_cls.return_value.apply_async.call_args
        assert kwargs["task_id"] == "backfill-run-1"
        # Jobs created for the open months are drift-source
        assert job_manager.objects.create.call_count == 2
        _, create_kwargs = job_manager.objects.create.call_args
        assert create_kwargs["is_drift_source"] is True

    def test_running_month_aborts_submit(self):
        snapshot_manager, job_manager, run_manager, _ = self._patch_world()
        running_job = MagicMock(status="running")
        running_job.started_at = None
        from django.utils import timezone as dj_tz
        running_job.created_at = dj_tz.now()  # fresh → blocks the submit
        job_manager.objects.filter.return_value.first.return_value = \
            running_job
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager):
            with pytest.raises(BackfillError) as exc:
                run_snapshot_backfill("Jamaica", "2024_01", "2024_03")
        assert "RUNNING" in str(exc.value)

    def test_all_months_completed_short_circuits(self):
        snapshot_manager, job_manager, run_manager, run = self._patch_world()
        completed_job = MagicMock(status="completed")
        job_manager.objects.filter.return_value.first.return_value = \
            completed_job
        chain_cls = MagicMock()
        with patch(
            "core.services.pipeline.run_dispatch_service.resolve_country_iso",
            return_value="JM",
        ), patch("osmsnapshot.models.Snapshot", snapshot_manager), patch(
            "osmsnapshot.models.SnapshotJob", job_manager,
        ), patch("core.models.PipelineRun", run_manager), patch(
            "celery.chain", chain_cls,
        ):
            result = run_snapshot_backfill("Jamaica", "2024_01", "2024_02")
        assert result["dispatched"] is False
        assert "already completed" in result["message"]
        run_manager.objects.create.assert_not_called()
        chain_cls.assert_not_called()


# =============================================================================
# 5. UpsertOnlyWriter — no encode, vector=None
# =============================================================================

class TestUpsertOnlyWriter:

    def test_add_line_passes_none_vector(self):
        from geovectors_encoder.services.snapshot_processors import (
            UpsertOnlyWriter,
        )
        storage = MagicMock()
        writer = UpsertOnlyWriter(storage)
        record = [123, "node", [("amenity", "cafe")], 18.0, -77.5]
        writer.add_line(record)
        storage.add.assert_called_once_with(record, None)

    def test_boundary_filters_outside_entities(self):
        from geovectors_encoder.services.snapshot_processors import (
            UpsertOnlyWriter,
        )
        storage = MagicMock()
        boundary = MagicMock()
        boundary.contains.return_value = False
        writer = UpsertOnlyWriter(storage, boundary=boundary)
        writer.add_line([123, "node", [("amenity", "cafe")], 0.0, 0.0])
        storage.add.assert_not_called()


# =============================================================================
# 6. Calendar payload — drift-source never offered as queryable
# =============================================================================

class TestCalendarPayloadTierFilter:

    def test_completed_dates_exclude_drift_source(self):
        from api.views.pipeline_status import build_snapshot_dates_payload

        job_manager = MagicMock()
        filter_chain = (
            job_manager.objects.filter.return_value
            .values_list.return_value.distinct.return_value
        )
        filter_chain.__iter__.return_value = iter(["2025_12_31"])
        with patch("osmsnapshot.models.SnapshotJob", job_manager):
            payload = build_snapshot_dates_payload()
        _, filter_kwargs = job_manager.objects.filter.call_args
        assert filter_kwargs.get("is_drift_source") is False
        assert payload["completed_dates"] == ["2025_12_31"]
