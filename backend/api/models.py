# Backward compatibility imports - models moved to extraction, orchestration, analysis apps
# This file maintains imports for existing code that references api.models
# Note: analysis.models will be migrated to extraction/orchestration in a future release

from extraction.models import (
    PbfFile,
    RegionHierarchy,
    PbfExtract,
    PolygonFile,
    PbfCollection
)

from orchestration.models import (
    ProcessingSession,
    Task,
    OsmiumDatasetMetrics,
    RegisteredService,
    CountryArtifact,
    EmbeddingArtifact,
    CountrySearchProcessing,
)

from analysis.models import (
    TemporalSnapshot,
    WorldKGClassDrift,
    WorldKGClassFingerprint,
    AssetBundle,
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
    'TemporalSnapshot',
    'WorldKGClassDrift',
    'WorldKGClassFingerprint',
    'AssetBundle',
    'PbfTagDistribution',
]
