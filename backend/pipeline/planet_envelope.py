"""Planet-init pipeline envelope types.

Split out of ``pipeline/envelopes.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN): PlanetRunState and
PlanetEnvelope.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional


# ──────────────────────────────────────────────────────────────────────────
# PlanetEnvelope (for planet-init tasks)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class PlanetRunState:
    """Mutable state for planet-init runs."""
    snapshot_date: str = "2025_12_31"
    continents_extracted: bool = False
    # Transient (NOT serialized by to_dict): artifact descriptors produced by
    # the task body, consumed by the @pipeline_step on_success hook to emit
    # PipelineAsset rows. See CountryRunState.pending_assets for the shape.
    pending_assets: List[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class PlanetEnvelope:
    """The message passed between planet-init Celery tasks (Steps 0–0.97).

    Simpler than CountryEnvelope — planet-init has no country identity, no
    per-country paths, no hyperparams, no subgraphs, and no StorageTarget
    (always writes to `default`).

    Fields:
        pipeline_run_id: UUID of the PipelineRun row.
        pbf_path: Path to the planet OSM PBF file (or None to use default).
        extract_continents: Whether Step 0.5 (continent extraction) runs.
        state: PlanetRunState (mutable) — snapshot_date, continents_extracted.

    Duck-typing: exposes `iso` ("PL"), `name` ("Planet"), `slug` ("planet"),
    `continent` ("planet"), and `snapshot_date` so the @pipeline_step decorator
    and task bodies can treat CountryEnvelope and PlanetEnvelope uniformly.
    """
    pipeline_run_id: str
    pbf_path: Optional[str] = None
    extract_continents: bool = True
    state: PlanetRunState = dataclasses.field(default_factory=PlanetRunState)

    # ── Compatibility with legacy CountryConfig field names ─────────────
    @property
    def iso(self) -> str:
        return "PL"

    @property
    def name(self) -> str:
        return "Planet"

    @property
    def slug(self) -> str:
        return "planet"

    @property
    def continent(self) -> str:
        return "planet"

    @property
    def snapshot_date(self) -> str:
        return self.state.snapshot_date

    def with_state(self, **kw) -> "PlanetEnvelope":
        new_state = dataclasses.replace(self.state, **kw)
        return dataclasses.replace(self, state=new_state)

    def to_dict(self) -> dict:
        return {
            "iso": "PL",
            "name": "Planet",
            "slug": "planet",
            "continent": "planet",
            "pipeline_run_id": self.pipeline_run_id,
            "pbf_path": self.pbf_path,
            "extract_continents": self.extract_continents,
            "snapshot_date": self.state.snapshot_date,
            "continents_extracted": self.state.continents_extracted,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanetEnvelope":
        return cls(
            pipeline_run_id=d.get("pipeline_run_id", ""),
            pbf_path=d.get("pbf_path"),
            extract_continents=d.get("extract_continents", True),
            state=PlanetRunState(
                snapshot_date=d.get("snapshot_date", "2025_12_31"),
                continents_extracted=d.get("continents_extracted", False),
            ),
        )
