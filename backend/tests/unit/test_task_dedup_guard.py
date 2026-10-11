"""Dedup guard regression.

Broker visibility-timeout redelivery can enqueue a second copy of a task
while the first is still running — the same task ID then executes twice
concurrently. The @pipeline_step dedup guard must make the second copy
skip its body and return the config dict unchanged so the chain can
continue.
"""

import logging

import pytest

from pipeline.task_decorator import pipeline_step


class _FakeEnvelope:
    @classmethod
    def from_dict(cls, config_dict):
        return _FakeEnvelope()


class _FakeTask:
    class _Request:
        id = "task-dedup-test-1"

    request = _Request()


class _FakeRedisClient:
    def __init__(self, acquired):
        self.acquired = acquired

    def set(self, *args, **kwargs):
        return self.acquired


def _wrap_body(body):
    return pipeline_step("test_step", _FakeEnvelope, 1.0)(body)


@pytest.mark.unit
def test_duplicate_execution_skips_body(monkeypatch, caplog):
    """SET NX losing (set→False) must skip the body and pass config through."""
    import redis as _redis

    monkeypatch.setattr(
        _redis.Redis, "from_url",
        classmethod(lambda cls, url: _FakeRedisClient(acquired=False)),
    )
    config = {"iso": "PL", "name": "Poland", "continent": "europe", "slug": "poland"}

    def body(self, env):
        raise AssertionError("task body must not run for a duplicate")

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = _wrap_body(body)(_FakeTask(), config)

    assert result == config
    assert "Duplicate execution of task task-dedup-test-1" in caplog.text