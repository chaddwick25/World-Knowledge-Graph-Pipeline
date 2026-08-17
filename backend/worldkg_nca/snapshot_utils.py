"""Snapshot partition-key helpers for Phase 6 temporal sharding.

This module clarifies the distinction between the two snapshot-related
fields on ``OsmEntity``:

- ``source_snapshot_id`` — UUID to ``osmsnapshot.Snapshot`` (default DB).
  This is the *cross-database reference* that has existed since the
  monolith was built.

- ``snapshot_id`` — VARCHAR(20) *partition key* in ``YYYY_MM_DD`` format
  (e.g. ``'2025_12_31'``).  This is the human-readable key used by the
  partitioned table (``embeddings_partitioned``) for ``LIST(snapshot_id)``
  partition pruning.  It is derived from ``source_snapshot_id`` →
  ``Snapshot.snapshot_date``.

The helpers below are used by every code path that needs to scope an
``OsmEntity`` query to a single snapshot partition.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# Default snapshot_id used when no Snapshot can be resolved.
DEFAULT_SNAPSHOT_ID = "2025_12_31"


def snapshot_id_from_uuid(snapshot_uuid: Optional[UUID]) -> Optional[str]:
    """Convert a ``Snapshot`` UUID to a ``YYYY_MM_DD`` partition key.

    Looks up the ``Snapshot`` row (in the *default* DB) and returns its
    ``snapshot_date`` field.  Returns ``None`` if the UUID is ``None`` or
    the snapshot cannot be found.

    Args:
        snapshot_uuid: UUID of a ``Snapshot`` row (i.e. the value
            of ``OsmEntity.source_snapshot_id``).

    Returns:
        Partition-key string like ``'2025_12_31'``, or ``None``.
    """
    if snapshot_uuid is None:
        return None
    try:
        from osmsnapshot.models import Snapshot

        snapshot = Snapshot.objects.using("default").filter(
            id=snapshot_uuid
        ).only("snapshot_date").first()
        if snapshot and snapshot.snapshot_date:
            return snapshot.snapshot_date
    except Exception as exc:
        logger.warning(
            "snapshot_id_from_uuid: failed to resolve UUID %s: %s",
            snapshot_uuid, exc,
        )
    return None


def get_latest_snapshot_id() -> Optional[str]:
    """Return the most recent non-NULL ``snapshot_id`` from the vectors DB.

    Queries ``OsmEntity`` (vectors DB) for the latest populated
    ``snapshot_id`` value.  Returns ``None`` if no rows have been
    backfilled yet (i.e. the monolith is still in pre-Phase-6 state).

    This is the fallback used by REST endpoints that do not receive an
    explicit ``snapshot_id`` query parameter.

    Returns:
        Partition-key string like ``'2025_12_31'``, or ``None``.
    """
    try:
        from worldkg_nca.models import OsmEntity

        return (
            OsmEntity.objects.using("vectors")
            .filter(snapshot_id__isnull=False)
            .exclude(snapshot_id="")
            .order_by("-snapshot_id")
            .values_list("snapshot_id", flat=True)
            .first()
        )
    except Exception as exc:
        logger.warning("get_latest_snapshot_id: failed: %s", exc)
        return None


def resolve_snapshot_id_for_request(request) -> Optional[str]:
    """Extract ``snapshot_id`` from a DRF request, falling back to latest.

    Checks the query parameter ``snapshot_id`` on the request.  If not
    present, falls back to :func:`get_latest_snapshot_id`.

    Args:
        request: A DRF ``Request`` object (must have ``query_params``).

    Returns:
        Partition-key string, or ``None`` if neither the param nor any
        backfilled row provides one.
    """
    snapshot_id = None
    query_params = getattr(request, "query_params", None) or {}
    if hasattr(query_params, "get"):
        snapshot_id = query_params.get("snapshot_id")
    return snapshot_id or get_latest_snapshot_id()
