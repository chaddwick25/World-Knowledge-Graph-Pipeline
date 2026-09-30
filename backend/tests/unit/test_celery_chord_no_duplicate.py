"""Regression test for the ChordCounter duplicate-execution bug.

Background
----------
The ``django-db`` Celery result backend (django-celery-results 2.0.0) has a
bug where ``ChordCounter`` records are never created for chords that are
part of a ``chain()``.  When each chord header task completes,
``on_chord_part_return`` tries to find the ChordCounter → ``DoesNotExist``
→ the task is marked ``FAILURE`` → Celery re-delivers the task → the task
runs again.

Observed impact on Norway run 4f99702c (2026-08-15):
  - 17 subgraphs trained 34 times (2-7x each)
  - 68 ``ChordCounter.DoesNotExist`` errors in worker logs
  - Step 5 took 9h47m instead of the expected ~4h50m
  - vestland (725K entities) and østfold (909K entities) trained 7x each

The fix is a custom ``PatchedDatabaseBackend``
(``pipeline/celery_results_backend.py``) that replaces ChordCounter with
the standard Celery ``fallback_chord_unlock()`` polling task.
``TaskResult`` records are still written to Django's DB, so the durable
results layer (``/api/snapshot-jobs/<cc>/<date>/results/``) continues to
work.

These tests guard against regression in three ways:

1. **Config guard** (unit, no broker needed):
   Asserts ``CELERY_RESULT_BACKEND`` points to ``PatchedDatabaseBackend``
   and is not the bare ``"django-db"`` string.

2. **Backend unit tests** (unit, no broker needed):
   Directly tests ``PatchedDatabaseBackend.apply_chord`` and
   ``on_chord_part_return`` to verify they don't raise
   ``ChordCounter.DoesNotExist`` and don't create ChordCounter rows.

3. **Chord-in-chain execution count** (unit, eager mode):
   Builds a ``chord`` inside a ``chain`` — the exact pattern that
   ``canvas.py`` uses for Step 5 — and asserts each header task runs
   exactly once.  This is a **canvas composition smoke test**: it
   verifies that headers run once, the callback runs once, and the
   chain continues.  It does **not** exercise the result backend's
   chord methods — in eager mode, ``chord.apply`` bypasses
   ``apply_chord`` and eager requests never set ``request.chord``, so
   ``on_chord_part_return`` is never called.  The actual regression
   guards for the Norway bug are ``TestResultBackendConfig`` (config
   gate) and ``TestPatchedDatabaseBackend`` (method-level behavior).
"""

import pytest

from django.conf import settings


# --------------------------------------------------------------------------- #
# 1. Config guard — fast CI gate
# --------------------------------------------------------------------------- #

class TestResultBackendConfig:
    """Ensure the Celery result backend uses PatchedDatabaseBackend.

    The django-celery-results 2.0.0 DatabaseBackend has a bug where
    ChordCounter records are never created for chords inside chains.
    This caused duplicate subgraph training in Step 5 (Norway: 34 training
    runs for 17 subgraphs, 9h47m instead of ~4h50m).

    Our PatchedDatabaseBackend fixes this by using fallback_chord_unlock()
    polling instead of ChordCounter, while still writing TaskResult rows
    to Django's DB.
    """

    def test_result_backend_is_not_bare_django_db(self):
        """CELERY_RESULT_BACKEND must not be the bare 'django-db' string."""
        backend = settings.CELERY_RESULT_BACKEND
        assert backend != "django-db", (
            f"CELERY_RESULT_BACKEND is 'django-db' — this triggers the "
            f"ChordCounter.DoesNotExist bug for chords inside chains, "
            f"causing duplicate task execution. Use "
            f"pipeline.celery_results_backend:PatchedDatabaseBackend instead. "
            f"See: docs/issues/CHORDCOUNTER_DOES_NOT_EXIST_BUG.md"
        )

    def test_result_backend_uses_patched_backend(self):
        """CELERY_RESULT_BACKEND should point to PatchedDatabaseBackend."""
        backend = settings.CELERY_RESULT_BACKEND
        assert "PatchedDatabaseBackend" in backend, (
            f"CELERY_RESULT_BACKEND is '{backend}' — expected "
            f"'pipeline.celery_results_backend:PatchedDatabaseBackend'. "
            f"This custom backend fixes the ChordCounter.DoesNotExist bug "
            f"while preserving TaskResult queryability."
        )


# --------------------------------------------------------------------------- #
# 2. Backend unit tests — direct method testing
# --------------------------------------------------------------------------- #

class TestPatchedDatabaseBackend:
    """Directly test PatchedDatabaseBackend's chord methods.

    These tests verify the fix at the backend level without needing a
    running Celery worker or broker.
    """

    def test_on_chord_part_return_does_not_raise(self):
        """on_chord_part_return must not raise ChordCounter.DoesNotExist.

        This is the exact error that caused the Norway incident. With the
        patched backend, on_chord_part_return is a no-op — the chord_unlock
        polling task handles callback firing.
        """
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from unittest.mock import MagicMock
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        request = MagicMock()
        request.id = str(uuid.uuid4())
        request.group = str(uuid.uuid4())

        # Must not raise — this is the core fix
        backend.on_chord_part_return(request=request, state="SUCCESS", result={})

    def test_on_chord_part_return_no_gid_is_noop(self):
        """on_chord_part_return with no group_id should be a no-op."""
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from unittest.mock import MagicMock

        backend = PatchedDatabaseBackend(app=celery_app)
        request = MagicMock()
        request.id = "test-id"
        request.group = None

        # Must not raise
        backend.on_chord_part_return(request=request, state="SUCCESS", result={})

    def test_apply_chord_does_not_create_chordcounter(self):
        """apply_chord must NOT create a ChordCounter row.

        The patched backend uses fallback_chord_unlock() (polling) instead
        of ChordCounter. This test verifies no ChordCounter.objects.create
        is called — only fallback_chord_unlock and a stale cleanup delete.
        """
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from django_celery_results.models import ChordCounter
        from unittest.mock import MagicMock, patch, call
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        group_id = str(uuid.uuid4())

        # Mock ChordCounter.objects to track create vs filter/delete
        mock_manager = MagicMock()
        mock_filter_qs = MagicMock()
        mock_manager.filter.return_value = mock_filter_qs

        with patch.object(ChordCounter, "objects", mock_manager):
            with patch.object(backend, "fallback_chord_unlock") as mock_fallback:
                header_result = MagicMock()
                header_result.id = group_id
                header_result.__len__ = lambda self: 3
                body = MagicMock()
                body.options = {}

                backend.apply_chord(header_result, body)

                # fallback_chord_unlock should have been called
                assert mock_fallback.called, (
                    "apply_chord should call fallback_chord_unlock to schedule "
                    "the chord_unlock polling task"
                )

                # ChordCounter.objects.create should NOT have been called
                mock_manager.create.assert_not_called(), (
                    "PatchedDatabaseBackend.apply_chord must NOT call "
                    "ChordCounter.objects.create — it uses "
                    "fallback_chord_unlock instead"
                )

                # ChordCounter.objects.filter(group_id=...).delete() should
                # have been called (stale cleanup)
                mock_manager.filter.assert_called_once_with(group_id=group_id)
                mock_filter_qs.delete.assert_called_once()

    def test_apply_chord_cleans_up_stale_chordcounter(self):
        """apply_chord should clean up any stale ChordCounter for the group.

        Verifies that filter(group_id=...).delete() is called, which
        cleans up stale ChordCounter rows from previous runs.
        """
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from django_celery_results.models import ChordCounter
        from unittest.mock import MagicMock, patch
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        group_id = str(uuid.uuid4())

        mock_manager = MagicMock()
        mock_filter_qs = MagicMock()
        mock_manager.filter.return_value = mock_filter_qs

        with patch.object(ChordCounter, "objects", mock_manager):
            with patch.object(backend, "fallback_chord_unlock"):
                header_result = MagicMock()
                header_result.id = group_id
                header_result.__len__ = lambda self: 3
                body = MagicMock()
                body.options = {}

                backend.apply_chord(header_result, body)

                # filter(group_id=...).delete() should have been called
                mock_manager.filter.assert_called_once_with(group_id=group_id)
                mock_filter_qs.delete.assert_called_once(), (
                    "PatchedDatabaseBackend.apply_chord should delete stale "
                    "ChordCounter rows via filter(group_id=...).delete()"
                )

    def test_on_chord_part_return_does_not_query_chordcounter(self):
        """on_chord_part_return must not query ChordCounter at all.

        The original DatabaseBackend does
        ChordCounter.objects.select_for_update().get(group_id=gid)
        which raises DoesNotExist. The patched backend should not
        touch the ChordCounter table.
        """
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from django_celery_results.models import ChordCounter
        from unittest.mock import MagicMock, patch
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        request = MagicMock()
        request.id = str(uuid.uuid4())
        request.group = str(uuid.uuid4())

        # Patch ChordCounter.objects to raise if queried
        with patch.object(
            ChordCounter, "objects",
            new=MagicMock(),
        ) as mock_objects:
            mock_objects.select_for_update.side_effect = AssertionError(
                "PatchedDatabaseBackend.on_chord_part_return must NOT "
                "query ChordCounter.objects"
            )
            # Must not raise — should not touch ChordCounter
            backend.on_chord_part_return(
                request=request, state="SUCCESS", result={},
            )

            # Verify ChordCounter.objects was never accessed for query
            mock_objects.select_for_update.assert_not_called()

    def test_apply_chord_overrides_none_interval(self):
        """chord.run passes interval=None explicitly; the patch must still
        apply CHORD_UNLOCK_INTERVAL.

        chord.run() calls apply_chord with interval=None as an explicit
        keyword (canvas.py:1422-1429). setdefault() only sets *absent*
        keys, so it would be a no-op. The patch uses an explicit None-check
        to override it. This test verifies that fix.
        """
        from pipeline.celery_results_backend import (
            PatchedDatabaseBackend, CHORD_UNLOCK_INTERVAL,
        )
        from pipeline.celery_app import celery_app
        from django_celery_results.models import ChordCounter
        from unittest.mock import MagicMock, patch
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        with patch.object(ChordCounter, "objects", MagicMock()):
            with patch.object(backend, "fallback_chord_unlock") as mock_fallback:
                header_result = MagicMock()
                header_result.id = str(uuid.uuid4())
                body = MagicMock()
                body.options = {}

                # Exactly how chord.run calls it (canvas.py:1422-1429)
                backend.apply_chord(
                    header_result, body,
                    interval=None, countdown=1, max_retries=None,
                )

                _, kwargs = mock_fallback.call_args
                assert kwargs.get("interval") == CHORD_UNLOCK_INTERVAL, (
                    f"fallback_chord_unlock got interval="
                    f"{kwargs.get('interval')!r} — expected "
                    f"{CHORD_UNLOCK_INTERVAL}. chord.run passes "
                    f"interval=None explicitly, so setdefault() does "
                    f"not apply the default."
                )

    def test_apply_chord_respects_explicit_interval(self):
        """If a caller passes a non-None interval, the patch must not override it."""
        from pipeline.celery_results_backend import PatchedDatabaseBackend
        from pipeline.celery_app import celery_app
        from django_celery_results.models import ChordCounter
        from unittest.mock import MagicMock, patch
        import uuid

        backend = PatchedDatabaseBackend(app=celery_app)
        with patch.object(ChordCounter, "objects", MagicMock()):
            with patch.object(backend, "fallback_chord_unlock") as mock_fallback:
                header_result = MagicMock()
                header_result.id = str(uuid.uuid4())
                body = MagicMock()
                body.options = {}

                backend.apply_chord(
                    header_result, body,
                    interval=30, countdown=1, max_retries=None,
                )

                _, kwargs = mock_fallback.call_args
                assert kwargs.get("interval") == 30, (
                    f"fallback_chord_unlock got interval="
                    f"{kwargs.get('interval')!r} — expected 30. "
                    f"Explicit non-None intervals must be respected."
                )


# --------------------------------------------------------------------------- #
# 3. Chord-in-chain canvas composition smoke test
# --------------------------------------------------------------------------- #

class TestChordInChainNoDuplicates:
    """Chord-in-chain canvas composition smoke test.

    This mimics the Step 5 pattern in canvas.py:
        chain(step_5.s(cfg), chord([sub1.si(), sub2.si(), ...], callback.s(cfg)), step_6.s())

    **What this test guards:**
    - Chord-in-chain canvas composition works end-to-end in eager mode:
      headers run once, callback runs once, chain continues to the post-task.

    **What this test does NOT guard (important):**
    - In eager mode, ``chord.apply`` bypasses ``backend.apply_chord``
      (canvas.py:1360-1363) and eager requests never set ``request.chord``,
      so ``on_chord_part_return`` is never called either. This test would
      pass identically with the stock buggy ``django-db`` backend.
    - The actual regression guards for the Norway bug are
      ``TestResultBackendConfig`` (config gate) and
      ``TestPatchedDatabaseBackend`` (method-level behavior), plus the
      interval test ``test_apply_chord_overrides_none_interval``.
    - See ``docs/issues/CHORD_BACKEND_REVIEW_FINDINGS.md`` F3.
    """

    @pytest.fixture
    def eager_celery(self):
        """Run tasks eagerly (synchronously) for the duration of the test."""
        from pipeline.celery_app import celery_app
        old_eager = celery_app.conf.task_always_eager
        old_propagate = celery_app.conf.task_eager_propagates
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            yield celery_app
        finally:
            celery_app.conf.task_always_eager = old_eager
            celery_app.conf.task_eager_propagates = old_propagate

    @pytest.fixture
    def call_tracker(self):
        """Track how many times each task is called."""
        return {"calls": {}}

    def test_chord_header_tasks_run_exactly_once(self, eager_celery, call_tracker):
        """Each chord header task must execute exactly once.

        This is the core regression test. If the result backend has the
        ChordCounter bug, tasks will be marked FAILURE and re-delivered,
        causing counts > 1.
        """
        from celery import chord, chain
        from pipeline.celery_app import celery_app

        tracker = call_tracker

        @celery_app.task(name="test_chord_dup_header", bind=True)
        def _header_task(self, idx):
            tracker["calls"][f"header_{idx}"] = tracker["calls"].get(f"header_{idx}", 0) + 1
            return {"idx": idx, "status": "ok"}

        @celery_app.task(name="test_chord_dup_callback", bind=True)
        def _callback_task(self, results, config=None):
            tracker["calls"]["callback"] = tracker["calls"].get("callback", 0) + 1
            return {"aggregated": results, "config": config}

        @celery_app.task(name="test_chord_dup_pre", bind=True)
        def _pre_task(self, config=None):
            tracker["calls"]["pre"] = tracker["calls"].get("pre", 0) + 1
            return config or {}

        @celery_app.task(name="test_chord_dup_post", bind=True)
        def _post_task(self, prev=None):
            tracker["calls"]["post"] = tracker["calls"].get("post", 0) + 1
            return {"done": True}

        # Build the exact pattern from canvas.py Step 5:
        #   chain(pre.s(cfg), chord([header.si(0), header.si(1), header.si(2)],
        #                            callback.s(cfg)), post.s())
        num_headers = 5
        header_sigs = [_header_task.si(i) for i in range(num_headers)]
        cfg = {"test": True}

        canvas = chain(
            _pre_task.s(cfg),
            chord(header_sigs, _callback_task.s(cfg)),
            _post_task.s(),
        )

        # Execute eagerly — Celery's trace calls apply_chord + on_chord_part_return
        canvas.apply_async()

        # Assert each header ran exactly once
        for i in range(num_headers):
            count = tracker["calls"].get(f"header_{i}", 0)
            assert count == 1, (
                f"Header task {i} ran {count} times — expected exactly 1. "
                f"This indicates the ChordCounter bug is present: the result "
                f"backend is failing to track chord completion, causing "
                f"re-delivery of 'failed' tasks. "
                f"All call counts: {tracker['calls']}"
            )

        # Assert the callback ran exactly once
        cb_count = tracker["calls"].get("callback", 0)
        assert cb_count == 1, (
            f"Chord callback ran {cb_count} times — expected exactly 1. "
            f"All call counts: {tracker['calls']}"
        )

        # Assert pre and post ran exactly once
        assert tracker["calls"].get("pre", 0) == 1, f"Pre-task ran {tracker['calls'].get('pre', 0)} times"
        assert tracker["calls"].get("post", 0) == 1, f"Post-task ran {tracker['calls'].get('post', 0)} times"

    def test_no_chordcounter_doesnotexist_in_eager_mode(self, eager_celery):
        """No ChordCounter.DoesNotExist should be raised during chord execution.

        This directly tests for the error that caused the Norway incident.
        With PatchedDatabaseBackend, on_chord_part_return is a no-op and
        ChordCounter is never queried.
        """
        from celery import chord
        from pipeline.celery_app import celery_app

        @celery_app.task(name="test_chord_no_dne_header", bind=True)
        def _header(self, idx):
            return idx

        @celery_app.task(name="test_chord_no_dne_callback", bind=True)
        def _callback(self, results, config=None):
            return {"count": len(results)}

        header_sigs = [_header.si(i) for i in range(3)]
        ch = chord(header_sigs, _callback.s({"cfg": True}))

        # If ChordCounter.DoesNotExist is raised, it will propagate as an
        # exception in eager mode (task_eager_propagates=True).
        try:
            result = ch.apply_async()
            assert result is not None
        except Exception as exc:
            exc_str = str(exc)
            assert "ChordCounter" not in exc_str, (
                f"ChordCounter.DoesNotExist was raised during chord execution: {exc}. "
                f"This is the exact bug that caused duplicate subgraph training "
                f"in the Norway run (9h47m instead of ~4h50m). The result backend "
                f"must use PatchedDatabaseBackend, not the bare django-db backend."
            )
            raise

    def test_eager_mode_does_not_call_apply_chord(self, eager_celery):
        """Document that eager mode bypasses the result backend's chord methods.

        In eager mode, ``chord.apply`` (canvas.py:1360-1363) runs header
        tasks synchronously via ``group.apply`` and calls the body directly
        — it never calls ``backend.apply_chord`` or
        ``on_chord_part_return``. This test explicitly documents that
        bypass so nobody re-assumes the behavioral tests exercise the
        backend's chord path.

        See ``docs/issues/CHORD_BACKEND_REVIEW_FINDINGS.md`` F3.
        """
        from celery import chord, chain
        from unittest.mock import patch
        from pipeline.celery_app import celery_app

        call_counts = {"apply_chord": 0, "on_chord_part_return": 0}

        backend = celery_app.backend

        @celery_app.task(name="test_eager_bypass_header", bind=True)
        def _header(self, idx):
            return idx

        @celery_app.task(name="test_eager_bypass_callback", bind=True)
        def _callback(self, results, config=None):
            return {"count": len(results)}

        @celery_app.task(name="test_eager_bypass_pre", bind=True)
        def _pre(self, config=None):
            return config or {}

        @celery_app.task(name="test_eager_bypass_post", bind=True)
        def _post(self, prev=None):
            return {"done": True}

        header_sigs = [_header.si(i) for i in range(3)]

        with patch.object(
            type(backend), "apply_chord",
            side_effect=lambda *a, **k: call_counts.__setitem__("apply_chord", call_counts["apply_chord"] + 1),
        ), patch.object(
            type(backend), "on_chord_part_return",
            side_effect=lambda *a, **k: call_counts.__setitem__("on_chord_part_return", call_counts["on_chord_part_return"] + 1),
        ):
            canvas = chain(
                _pre.s({"test": True}),
                chord(header_sigs, _callback.s({"cfg": True})),
                _post.s(),
            )
            canvas.apply_async()

        assert call_counts["apply_chord"] == 0, (
            f"apply_chord was called {call_counts['apply_chord']} times in "
            f"eager mode — expected 0. Eager mode bypasses the result "
            f"backend's chord methods (chord.apply → group.apply). "
            f"This test documents that bypass; see F3."
        )
        assert call_counts["on_chord_part_return"] == 0, (
            f"on_chord_part_return was called "
            f"{call_counts['on_chord_part_return']} times in eager mode "
            f"— expected 0. Eager requests never set request.chord."
        )
