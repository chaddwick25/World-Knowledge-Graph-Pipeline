# Backward compatibility imports - models moved to extraction, orchestration, osmsnapshot apps
# This file maintains imports for existing code that references api.models

from core.models import (
    PbfFile,
    RegionHierarchy,
    PbfExtract,
    PolygonFile,
    PbfCollection
)

from core.models import (
    ProcessingSession,
    Task,
    OsmiumDatasetMetrics,
    RegisteredService,
    CountryArtifact,
    EmbeddingArtifact,
    CountrySearchProcessing,
)

from osmsnapshot.models import (
    Snapshot,
    SnapshotJob,
    WorldKGClassDrift,
    WorldKGClassFingerprint,
    PbfTagDistribution,
)

# Export all models for backward compatibility
__all__ = [
    'PbfFile',
    'RegionHierarchy',
    'PbfExtract',
    'PolygonFile',
    'PbfCollection',
    'ProcessingSession',
    'Task',
    'OsmiumDatasetMetrics',
    'RegisteredService',
    'CountryArtifact',
    'EmbeddingArtifact',
    'CountrySearchProcessing',
    'Snapshot',
    'SnapshotJob',
    'WorldKGClassDrift',
    'WorldKGClassFingerprint',
    'PbfTagDistribution',
]
