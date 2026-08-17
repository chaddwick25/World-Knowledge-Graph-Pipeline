"""Core app models — consolidated from `extraction` and `orchestration`.

All models that previously lived in the `extraction` and `orchestration` apps
are now defined in `core`. Their Django app_label is `core`; the underlying
``db_table`` names are unchanged so existing data is preserved across the
consolidation (see ``docs/plans/CORE_APP_CONSOLIDATION_PLAN.md``).

Module layout matches the old `orchestration/models/` split, plus
``extraction.py`` and ``hierarchy.py`` for the former `extraction` models.
"""

from core.models.extraction import (
    PbfFile,
    RegionHierarchy,
    OsmBoundary,
    PbfExtract,
    PolygonFile,
    PbfCollection,
    RegionalExtractionState,
    ProjectionWeightAsset,
    PlanetaryMetrics,
)
from core.models.hierarchy import (
    OSMWikiDataHierarchy,
)
from core.models.infrastructure import (
    ProcessingSession,
    Task,
    OsmiumDatasetMetrics,
    RegisteredService,
    PlanetSnapshot,
    CountrySearchProcessing,
)
from core.models.pipeline import (
    PipelineRun,
    PipelineLogEntry,
    PipelineAsset,
)
from core.models.country_profile import (
    CountryRelationSnapshot,
    ContinentProfile,
    CountryPipelineProfile,
    SubgraphProfile,
    EligibleCountry,
)
from core.models.artifact import (
    CountryArtifact,
    EmbeddingArtifact,
    PartitionRegistry,
    ReasoningWorkflow,
    ReasoningStep,
)

# Signal handler must be imported to register the receiver
from core.models.infrastructure import update_processing_metrics

__all__ = [
    # extraction
    'PbfFile', 'RegionHierarchy', 'OsmBoundary', 'PbfExtract', 'PolygonFile',
    'PbfCollection', 'RegionalExtractionState', 'ProjectionWeightAsset',
    'PlanetaryMetrics',
    # hierarchy
    'OSMWikiDataHierarchy',
    # infrastructure
    'ProcessingSession', 'Task', 'OsmiumDatasetMetrics', 'RegisteredService',
    'PlanetSnapshot', 'CountrySearchProcessing',
    # pipeline
    'PipelineRun', 'PipelineLogEntry', 'PipelineAsset',
    # country_profile
    'CountryRelationSnapshot', 'ContinentProfile', 'CountryPipelineProfile',
    'SubgraphProfile', 'EligibleCountry',
    # artifact
    'CountryArtifact', 'EmbeddingArtifact', 'PartitionRegistry',
    'ReasoningWorkflow', 'ReasoningStep',
    # signals
    'update_processing_metrics',
]
