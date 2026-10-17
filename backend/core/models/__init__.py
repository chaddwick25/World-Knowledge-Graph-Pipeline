"""Core app models — consolidated from `extraction` and `orchestration`.

All models that previously lived in the `extraction` and `orchestration` apps
are now defined in `core`. Their Django app_label is `core`; the underlying
``db_table`` names are unchanged so existing data is preserved across the
consolidation.

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
from core.models.country_request import (
    CountryRequest,
)
from core.models.research_run import (
    ResearchRun,
)

***REMOVED*** Signal handler must be imported to register the receiver
from core.models.infrastructure import update_processing_metrics

__all__ = [
    ***REMOVED*** extraction
    'PbfFile', 'RegionHierarchy', 'OsmBoundary', 'PbfExtract', 'PolygonFile',
    'PbfCollection', 'RegionalExtractionState', 'ProjectionWeightAsset',
    'PlanetaryMetrics',
    ***REMOVED*** hierarchy
    'OSMWikiDataHierarchy',
    ***REMOVED*** infrastructure
    'ProcessingSession', 'Task', 'OsmiumDatasetMetrics', 'RegisteredService',
    'PlanetSnapshot', 'CountrySearchProcessing',
    ***REMOVED*** pipeline
    'PipelineRun', 'PipelineLogEntry', 'PipelineAsset',
    ***REMOVED*** country_profile
    'CountryRelationSnapshot', 'ContinentProfile', 'CountryPipelineProfile',
    'SubgraphProfile', 'EligibleCountry',
    ***REMOVED*** artifact
    'CountryArtifact', 'EmbeddingArtifact', 'PartitionRegistry',
    'ReasoningWorkflow', 'ReasoningStep',
    ***REMOVED*** country_request
    'CountryRequest',
    ***REMOVED*** research_run
    'ResearchRun',
    ***REMOVED*** signals
    'update_processing_metrics',
]
