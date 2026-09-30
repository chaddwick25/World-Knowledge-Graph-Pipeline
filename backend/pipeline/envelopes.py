"""Pipeline envelopes — frozen dataclass graphs passed between Celery tasks.

The envelope is the **message** passed between tasks. It carries *addresses*
of DB data (snapshot_id, country_code) but never the data itself.

Two envelope types, one per pipeline flavor:

    CountryEnvelope (frozen except state) — country pipeline (Steps 1–6)
    ├── identity: CountryIdentity (frozen) — iso, name, slug, continent, qids
    ├── paths: CountryPaths (frozen) — resolved filesystem paths
    ├── hyperparams: ModelHyperparams (frozen, from YAML) — USLP/DeepWalk/entropy
    ├── subgraphs: tuple[SubgraphConfig, ...] (frozen-safe)
    ├── state: CountryRunState (mutable) — pipeline_run_id, igea_accepted, etc.
    └── storage: StorageTarget (frozen) — which DB shard to query

    PlanetEnvelope (frozen except state) — planet-init pipeline (Steps 0–0.97)
    ├── pipeline_run_id: str
    ├── pbf_path: Optional[str] — planet PBF path
    ├── extract_continents: bool — whether to run continent extraction
    └── state: PlanetRunState (mutable) — snapshot_date, continents_extracted

    PlanetEnvelope is simpler than CountryEnvelope: planet-init has no country
    identity, no per-country paths, no hyperparams, no subgraphs, and no
    StorageTarget (always writes to `default`). It exposes the same `iso` /
    `name` / `slug` / `continent` / `snapshot_date` accessors as
    CountryEnvelope so the @pipeline_step decorator and task bodies can treat
    both envelopes uniformly (duck-typed).

Serialization:
    to_dict() serializes identity + paths + state + subgraphs + storage
    (CountryEnvelope) or pipeline_run_id + pbf_path + extract_continents +
    state (PlanetEnvelope). Hyperparams are NOT serialized — they're a named
    constant re-attached at from_dict() time.

Compatibility:
    to_dict() output uses the SAME key names as the legacy CountryConfig.to_dict()
    so the transition (Phases 2-5) is transparent to downstream tasks.

Monolith split (Phase 5 of PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN):
    The classes moved to per-family modules — pipeline/hyperparams.py,
    pipeline/country_envelope.py, pipeline/planet_envelope.py.  This module
    re-exports them so existing import sites keep working.
"""

from pipeline.country_envelope import (
    CountryEnvelope,
    CountryIdentity,
    CountryPaths,
    CountryRunState,
    StorageTarget,
)
from pipeline.hyperparams import (
    ModelHyperparams,
    _HYPERPARAMS_CACHE,
    _as_comma_str,
)
from pipeline.planet_envelope import (
    PlanetEnvelope,
    PlanetRunState,
)

__all__ = [
    "CountryEnvelope",
    "CountryIdentity",
    "CountryPaths",
    "CountryRunState",
    "StorageTarget",
    "ModelHyperparams",
    "PlanetEnvelope",
    "PlanetRunState",
]
