"""Custom Celery result backend: DatabaseBackend with chord-in-chain fix.

The django-celery-results 2.0.0 ``DatabaseBackend`` has a bug where
``ChordCounter`` records are not reliably created/visible for chords
embedded in a ``chain()``.  When each chord header task completes,
``on_chord_part_return`` tries ``ChordCounter.objects.get(group_id=gid)``
→ ``DoesNotExist`` → task marked ``FAILURE`` → Celery re-delivers the
task → duplicate execution.

See ``docs/issues/CHORDCOUNTER_DOES_NOT_EXIST_BUG.md`` for the full
analysis.

This backend fixes the bug by:

1. **``apply_chord``**: Uses ``fallback_chord_unlock()`` (the standard
   Celery polling approach from ``BaseBackend``) instead of creating a
   ``ChordCounter`` row.  The ``celery.chord_unlock`` task polls
   ``GroupResult.ready()`` and fires the callback when all header tasks
   are complete.

2. **``on_chord_part_return``**: Becomes a no-op.  ``TaskResult`` is
   already stored by ``store_result()`` in ``mark_as_done()`` *before*
   this method is called, so the ``chord_unlock`` polling task can
   detect task completion by querying ``TaskResult`` rows.

``TaskResult`` records are still written to Django's DB by
``store_result()``, so ``/api/snapshot-jobs/<country>/<date>/results/``
continues to work.

The polling interval defaults to 10 seconds (vs Celery's 1-second
default) to reduce Redis message volume during long GPU training runs
(3-10 minutes per subgraph).
"""

from __future__ import annotations

from celery.utils.log import get_logger

from django_celery_results.backends.database import DatabaseBackend
from django_celery_results.models import ChordCounter

logger = get_logger(__name__)

# Polling interval for chord_unlock task (seconds).  Celery's default is
# 1s, which creates ~180-600 retry messages for 3-10 minute GPU tasks.
# 10s reduces this to ~18-60 retries while keeping callback latency low.
CHORD_UNLOCK_INTERVAL = 10


class PatchedDatabaseBackend(DatabaseBackend):
    """DatabaseBackend that uses ``chord_unlock`` polling instead of ChordCounter.

    The ChordCounter mechanism in django-celery-results 2.0.0 has a bug
    where ChordCounter records are not reliably created for chords inside
    chains, causing ``DoesNotExist`` errors and duplicate task execution.

    This backend replaces ChordCounter with the standard Celery
    ``fallback_chord_unlock()`` polling task, which is the same mechanism
    used by the base ``BaseBackend`` class (and compatible with all Celery
    versions).

    ``TaskResult`` records are still written to Django's DB, so the
    ``/api/snapshot-jobs/<country>/<date>/results/`` endpoint continues
    to work.
    """

    def apply_chord(self, header_result, body, **kwargs):
        """Schedule ``chord_unlock`` polling task instead of creating ChordCounter.

        The ``chord_unlock`` task polls ``GroupResult.ready()`` and fires
        the callback when all header tasks are complete.  This is the
        standard Celery fallback used by ``BaseBackend.apply_chord()``.

        We also clean up any stale ``ChordCounter`` rows for this group
        (from a previous run or a partial run) to prevent confusion.
        """
        # Clean up stale ChordCounter from previous runs
        ChordCounter.objects.filter(group_id=header_result.id).delete()

        # Set a longer polling interval to reduce Redis message volume.
        # chord.run() passes interval=None explicitly (canvas.py:1422-1429),
        # so setdefault() — which only sets *absent* keys — would be a no-op.
        # Use an explicit None-check instead.
        if kwargs.get("interval") is None:
            kwargs["interval"] = CHORD_UNLOCK_INTERVAL

        # Use the standard fallback: schedule chord_unlock polling task
        self.fallback_chord_unlock(header_result, body, **kwargs)

    def on_chord_part_return(self, request, state, result, **kwargs):
        """No-op: ``chord_unlock`` polling task handles callback firing.

        ``TaskResult`` is already stored by ``store_result()`` in
        ``mark_as_done()`` *before* this method is called.  The
        ``chord_unlock`` polling task will detect completion by querying
        ``TaskResult`` rows and fire the callback when all header tasks
        are ready.

        Overriding this as a no-op eliminates the
        ``ChordCounter.DoesNotExist`` error that caused duplicate task
        execution (Norway: 34 training runs for 17 subgraphs).
        """
        tid, gid = request.id, request.group
        if not gid or not tid:
            return
        # TaskResult has already been stored by store_result() in
        # mark_as_done().  The chord_unlock polling task will detect
        # completion and fire the callback.  Nothing to do here.
        logger.debug(
            "on_chord_part_return: task %s completed for group %s "
            "(chord_unlock polling will handle callback)",
            tid, gid,
        )
