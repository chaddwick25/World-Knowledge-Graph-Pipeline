"""run_pipeline --stage fractional-key coverage.

The CLI must reach every stage registered in the Celery canvas, including
the fractional keys (1.5, 4.5, 5.5 chord callbacks, 5.7 graph spectral,
5.8 temporal drift). Regression for the control-plane gap where --stage
was hardcoded to int choices [1, 2, 3, 4, 5].
"""

import pytest
from django.core.management.base import CommandError

from pipeline.management.commands.run_pipeline import Command

VALID_STAGES = [1, 1.5, 2, 3, 4, 4.5, 5, 5.5, 5.7, 5.8, 6]


def _parse(argv):
    parser = Command().create_parser('manage.py', 'run_pipeline')
    return parser.parse_args(argv)


@pytest.mark.parametrize('stage', VALID_STAGES)
def test_stage_choice_accepted(stage):
    args = _parse(['MZ', '--stage', str(stage)])
    assert args.stage == float(stage)


@pytest.mark.parametrize('stage', [0, 0.5, 7, 9, 5.6])
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
