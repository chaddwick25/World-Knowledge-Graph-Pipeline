"""run_pipeline --stage fractional-key coverage.

The CLI must reach every stage registered in the Celery canvas, including
the fractional keys (1.5, 4.5, 5.5 chord callbacks, 5.7 graph spectral).
5.8 (temporal drift) was removed 2026-09-26 — scheduled-interval feature,
not a per-run step. Regression for the control-plane gap where --stage
was hardcoded to int choices [1, 2, 3, 4, 5].
"""

import pytest
from django.core.management.base import CommandError

from pipeline.management.commands.run_pipeline import Command

VALID_STAGES = [1, 1.5, 2, 3, 4, 4.5, 5, 5.5, 5.7, 6]


def _parse(argv):
    parser = Command().create_parser('manage.py', 'run_pipeline')
    return parser.parse_args(argv)


@pytest.mark.parametrize('stage', VALID_STAGES)
def test_stage_choice_accepted(stage):
    args = _parse(['MZ', '--stage', str(stage)])
    assert args.stage == float(stage)


@pytest.mark.parametrize('stage', [0, 0.5, 7, 9, 5.6, 5.8])
def test_stage_choice_rejected(stage):
    with pytest.raises(CommandError):
        _parse(['MZ', '--stage', str(stage)])


def test_step_task_keys_cover_cli_choices():
    """Every CLI stage maps to a registered canvas task (dict lookup uses
    numeric equality, so int keys 1-6 are found by float args)."""
    from pipeline.canvas import _get_step_tasks

    steps = _get_step_tasks()
    for stage in VALID_STAGES:
        assert steps.get(stage) is not None, f"stage {stage} has no task"


@pytest.mark.django_db(transaction=False, databases=["default"])
def test_single_stage_completion_marks_the_run_completed():
    """A --stage run has no later step to close it; the completion hook must
    set the whole run COMPLETED instead of leaving it RUNNING."""
    from core.models import PipelineRun
    from pipeline.task_decorator import _update_run_stage

    run = PipelineRun.objects.create(
        country_code="JM",
        country_name="Jamaica",
        pipeline_type="single_stage",
        status=PipelineRun.PipelineStatus.RUNNING,
    )
    _update_run_stage(str(run.id), "predict_spatial_links", completed=True)
    run.refresh_from_db()

    assert run.status == PipelineRun.PipelineStatus.COMPLETED
    assert "predict_spatial_links" in run.completed_stages
    assert run.completed_at is not None
    run.delete()


def test_wait_refuses_a_queue_with_no_consumer(monkeypatch):
    """--wait must fail fast when the routed queue has no worker."""
    from django.core.management import call_command

    monkeypatch.setenv("K80_GPU_QUEUE", "k80-gpu")
    monkeypatch.setattr(
        "pipeline.management.commands.run_pipeline._queue_has_consumer",
        lambda queue, timeout=2.0: False,
    )

    with pytest.raises(CommandError, match="No Celery worker is consuming"):
        call_command(
            "run_pipeline", "JM", "--stage", "4", "--wait",
            "--wait-timeout", "5",
        )


def test_wait_timeout_is_configurable():
    args = _parse(["MZ", "--wait", "--wait-timeout", "60"])
    assert args.wait_timeout == 60
