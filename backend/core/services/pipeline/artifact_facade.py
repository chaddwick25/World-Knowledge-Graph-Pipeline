"""Artifact Facade — the only layer that touches the PipelineAsset ORM.

Implements Phase 3 of ``docs/plans/TEMPORAL_SHARDING_ARTIFACT_PLAN.md`` and
§11.3 of ``docs/plans/GEO_SPATIAL_AGENT_CELERY_PLAN_V3.md``.

Design rules (v3 §11.5):

- **Frozen** return types — ``@dataclass(frozen=True)``; operators can pass
  them around without mutation.
- **``tuple`` not ``list``** for frozen-safety.
- **``dict`` copied**, not referenced, so the frozen dataclass holds a
  snapshot, not a live reference to the ORM field.
- **NumPy is optional** — ``EmbeddingSlice.embeddings`` is typed
  ``Optional[np.ndarray]``. If numpy is unavailable (or the artifact is not
  a numeric array), it stays ``None`` and the caller falls back to the
  ``storage_path``. This keeps the facade importable in minimal envs.

Shard routing (v3 §11.2):

- The facade resolves ``country_code -> continent -> db_alias`` once at
  construction (cached for the instance lifetime).
- All ORM queries go through ``.objects.using(self._shard)``. When sharding
  is not active, ``self._shard`` is ``"default"`` and the query is a no-op
  pass-through (the ShardRouter still routes by app_label to ``default``).
- Cross-shard fan-out (``get_temporal_series``) is out of scope for this
  initial implementation; it is stubbed with a clear ``NotImplementedError``
  so callers know it is intentional.

Construction:

    facade = ArtifactFacade.for_country(iso="BZ")
    handle = facade.get_artifact_handle(asset_type="GV_TAGS_EMBEDDING")

or, equivalently, with an envelope (the operator path):

    facade = ArtifactFacade.from_envelope(operator_envelope)

The envelope-based constructor is the one the agent uses; the
``for_country`` / ``for_run`` constructors are conveniences for REST
endpoints and management commands that do not have an envelope handy.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# NumPy is optional — the facade degrades gracefully without it.
try:
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is a hard dep in practice
    np = None  # type: ignore
    _HAS_NUMPY = False


# ── Frozen dataclasses (the facade's return types) ──────────────────────────


@dataclasses.dataclass(frozen=True)
class ArtifactHandle:
    """A reference to a materialized artifact — the facade's return type.

    Frozen so operators can pass it around without mutation. Carries enough
    metadata for provenance logging without re-querying the DB.
    """

    artifact_id: str               # PipelineAsset.id (UUID hex)
    asset_type: str                # PipelineAsset.AssetType value
    country_code: str
    continent: str
    snapshot_date: str             # may be "" if not snapshot-scoped
    storage_type: str
    storage_path: str              # filesystem path or vector DB reference
    record_count: int
    file_size_bytes: Optional[int]
    stage_name: str
    status: str
    metadata: dict                 # frozen copy of PipelineAsset.metadata

    @classmethod
    def from_asset(cls, asset, continent: str, country_code: str,
                   snapshot_date: str = "") -> "ArtifactHandle":
        """Build a frozen handle from a PipelineAsset ORM instance.

        Copies ``metadata`` so the frozen dataclass holds a snapshot, not a
        live reference to the ORM JSONField.
        """
        return cls(
            artifact_id=str(asset.id),
            asset_type=asset.asset_type,
            country_code=country_code,
            continent=continent,
            snapshot_date=snapshot_date,
            storage_type=asset.storage_type,
            storage_path=asset.storage_path,
            record_count=int(asset.record_count or 0),
            file_size_bytes=asset.file_size_bytes,
            stage_name=asset.stage_name or "",
            status=asset.status,
            metadata=dict(asset.metadata or {}),
        )

    def to_dict(self) -> dict:
        """JSON-serializable form (for REST endpoints / serializers)."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class EmbeddingSlice:
    """A materialized slice of embeddings — what operators actually compute on.

    The facade loads this from the shard DB or from a ``.npy`` file on disk,
    depending on ``storage_type``. Operators never see the ORM model.

    ``embeddings`` is a NumPy array when numpy is available and the artifact
    is a numeric array on disk; otherwise ``None`` (the caller falls back to
    ``handle.storage_path``). Treated as read-only by convention.
    """

    handle: ArtifactHandle
    osm_ids: Tuple[int, ...]       # tuple, not list — frozen-safe
    embeddings: Any                # np.ndarray | None
    latent_space: str              # "semantic" | "geographic" | "spectral" | ...

    def to_dict(self) -> dict:
        """JSON-serializable metadata (embeddings array excluded)."""
        return {
            "handle": self.handle.to_dict(),
            "osm_ids": list(self.osm_ids),
            "embeddings_loaded": self.embeddings is not None,
            "embedding_count": int(self.embeddings.shape[0]) if _HAS_NUMPY and self.embeddings is not None else 0,
            "latent_space": self.latent_space,
        }


# ── Continent -> shard alias ────────────────────────────────────────────────

# Mirrors GEO_SPATIAL_AGENT_CELERY_PLAN_V3.md §11.2. Keep in sync with
# ShardRouter._continent_to_shard when that router is activated (Phase 6).
_CONTINENT_TO_SHARD_ALIAS = {
    "africa": "shard_africa",
    "asia": "shard_asia",
    "europe": "shard_europe",
    "north-america": "shard_north_america",
    "north_america": "shard_north_america",
    "south-america": "shard_south_america",
    "south_america": "shard_south_america",
    "central-america": "shard_central_america",
    "central_america": "shard_central_america",
    "oceania": "shard_oceania",
    "russia": "shard_russia",
}


def continent_to_shard_alias(continent: str) -> str:
    """Map a continent slug to its shard DB alias.

    Returns ``"default"`` when the continent is unknown or empty — this is
    the safe pre-sharding fallback (the ShardRouter is a no-op today, so
    routing to ``default`` is correct and never raises).
    """
    if not continent:
        return "default"
    key = continent.strip().lower().replace(" ", "-")
    return _CONTINENT_TO_SHARD_ALIAS.get(key, "default")


# ── The facade ──────────────────────────────────────────────────────────────


class ArtifactFacade:
    """The only layer that touches the PipelineAsset ORM.

    Operators call this, not ``.objects``. Shard routing is transparent: the
    facade reads continent from the envelope's identity (or a country code)
    and passes ``db_alias`` to ``.objects.using()``.

    Construction is cheap; one DB hit to resolve continent (cached on the
    instance). All subsequent queries go to the resolved shard.
    """

    def __init__(self, country_code: str, continent: str,
                 snapshot_date: str = "", shard: Optional[str] = None) -> None:
        self._country_code = country_code or ""
        self._continent = continent or ""
        self._snapshot_date = snapshot_date or ""
        # Allow callers to force a shard (e.g. tests, cross-shard admin).
        self._shard = shard or continent_to_shard_alias(self._continent)

    # ── Constructors ──────────────────────────────────────────────────────

    @classmethod
    def for_country(cls, iso: str, snapshot_date: str = "") -> "ArtifactFacade":
        """Build a facade for a country, resolving continent from the DB.

        Resolves ``CountryPipelineProfile.country_relations_payload['continent']``
        once. Falls back to an empty continent (→ ``default`` shard) when the
        profile is missing — the facade stays usable, just not shard-routed.
        """
        continent = cls._resolve_continent(iso)
        return cls(country_code=iso, continent=continent,
                   snapshot_date=snapshot_date)

    @classmethod
    def for_run(cls, pipeline_run_id: str) -> "ArtifactFacade":
        """Build a facade scoped to a specific PipelineRun.

        Reads ``PipelineRun.country_code`` then resolves continent. Useful for
        REST endpoints that take a ``run_id`` path parameter.
        """
        from core.models import PipelineRun
        run = PipelineRun.objects.filter(id=pipeline_run_id).first()
        if run is None:
            raise ValueError(f"PipelineRun {pipeline_run_id} not found")
        continent = cls._resolve_continent(run.country_code)
        snapshot_date = ""
        # CountryPipelineProfile may carry a snapshot_date; prefer it when present.
        profile = cls._resolve_profile(run.country_code)
        if profile is not None:
            snapshot_date = profile.snapshot_date or ""
        return cls(country_code=run.country_code, continent=continent,
                   snapshot_date=snapshot_date)

    @classmethod
    def from_envelope(cls, envelope) -> "ArtifactFacade":
        """Build a facade from an OperatorEnvelope (the agent/operator path).

        The envelope is expected to expose ``identity.country_code`` and
        ``identity.continent`` (the OperatorEnvelope shape from v3 §2.3). For
        convenience, a ``CountryEnvelope`` also works because it exposes
        ``.iso`` and ``.continent`` accessors.
        """
        identity = getattr(envelope, "identity", None)
        country_code = ""
        continent = ""
        if identity is not None:
            country_code = getattr(identity, "country_code", None) or getattr(identity, "iso", "") or ""
            continent = getattr(identity, "continent", "") or ""
        if not country_code:
            # CountryEnvelope / PlanetEnvelope flat accessors
            country_code = getattr(envelope, "iso", "") or ""
            continent = getattr(envelope, "continent", "") or ""
        snapshot_date = getattr(envelope, "snapshot_date", "") or ""
        if not continent and country_code:
            continent = cls._resolve_continent(country_code)
        return cls(country_code=country_code, continent=continent,
                   snapshot_date=snapshot_date)

    # ── Continent resolution (one DB hit, cached by caller via construction) ──

    @staticmethod
    def _resolve_profile(country_code: str):
        """Return the CountryPipelineProfile for a country, or None."""
        if not country_code:
            return None
        from core.models import CountryPipelineProfile
        # iso2 and iso3 are both indexed; try both.
        profile = (
            CountryPipelineProfile.objects.filter(iso2=country_code).first()
            or CountryPipelineProfile.objects.filter(iso3=country_code).first()
        )
        return profile

    @staticmethod
    def _resolve_continent(country_code: str) -> str:
        """Resolve country -> continent via CountryPipelineProfile.

        Reads ``country_relations_payload['continent']`` first, then falls
        back to ``continent_name``. Returns "" when unresolved.
        """
        profile = ArtifactFacade._resolve_profile(country_code)
        if profile is None:
            return ""
        payload = profile.country_relations_payload or {}
        return (
            payload.get("continent")
            or profile.continent_name
            or ""
        )

    # ── Read API ──────────────────────────────────────────────────────────

    @property
    def shard(self) -> str:
        """The resolved shard DB alias (read-only view for diagnostics)."""
        return self._shard

    @property
    def continent(self) -> str:
        return self._continent

    def list_artifacts(
        self,
        asset_type: Optional[str] = None,
        stage_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ArtifactHandle]:
        """List artifacts matching the filter, scoped to this facade's country.

        Returns frozen ``ArtifactHandle`` instances — no ORM objects leak out.
        Results are ordered by ``-created_at`` (newest first).
        """
        from core.models import PipelineAsset
        qs = PipelineAsset.objects.using(self._shard).filter(
            pipeline_run__country_code=self._country_code,
        )
        if asset_type:
            qs = qs.filter(asset_type=asset_type)
        if stage_name:
            qs = qs.filter(stage_name=stage_name)
        if status:
            qs = qs.filter(status=status)
        qs = qs.order_by("-created_at")
        if limit is not None:
            qs = qs[: int(limit)]
        return [
            ArtifactHandle.from_asset(a, continent=self._continent,
                                      country_code=self._country_code,
                                      snapshot_date=self._snapshot_date)
            for a in qs
        ]

    def get_artifact_handle(
        self,
        asset_type: str,
        snapshot_date: Optional[str] = None,
    ) -> Optional[ArtifactHandle]:
        """Fetch the most recent completed artifact of a given type.

        Returns ``None`` if no matching completed asset exists (operators
        should treat this as "artifact not yet produced" and short-circuit).
        """
        from core.models import PipelineAsset
        asset = (
            PipelineAsset.objects.using(self._shard)
            .filter(
                pipeline_run__country_code=self._country_code,
                asset_type=asset_type,
                status=PipelineAsset.AssetStatus.COMPLETED,
            )
            .order_by("-completed_at", "-created_at")
            .first()
        )
        if asset is None:
            return None
        return ArtifactHandle.from_asset(
            asset, continent=self._continent,
            country_code=self._country_code,
            snapshot_date=snapshot_date or self._snapshot_date,
        )

    def resolve_artifacts(
        self,
        snapshot: Optional[str] = None,
        subdivision: Optional[str] = None,
        step: Optional[str] = None,
        asset_type: Optional[str] = None,
    ) -> List[ArtifactHandle]:
        """Resolve artifacts for a ``(snapshot, country, subdivision, step, type)`` scope.

        Implements the sharding-key lookup from
        ``TEMPORAL_SHARDING_ARTIFACT_PLAN.md`` §"Sharding key for artifacts".
        ``snapshot`` and ``subdivision`` filter on ``PipelineAsset.metadata``
        (the registry is not physically partitioned; the key is indexed via
        the metadata JSON). Returns frozen handles.
        """
        from core.models import PipelineAsset
        qs = PipelineAsset.objects.using(self._shard).filter(
            pipeline_run__country_code=self._country_code,
        )
        if asset_type:
            qs = qs.filter(asset_type=asset_type)
        if step:
            qs = qs.filter(stage_name=step)
        if snapshot:
            qs = qs.filter(metadata__snapshot_id=snapshot)
        if subdivision:
            qs = qs.filter(metadata__subdivision=subdivision)
        qs = qs.order_by("-created_at")
        return [
            ArtifactHandle.from_asset(a, continent=self._continent,
                                      country_code=self._country_code,
                                      snapshot_date=snapshot or self._snapshot_date)
            for a in qs
        ]

    def get_embedding_slice(
        self,
        asset_type: str,
        snapshot_date: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Optional[EmbeddingSlice]:
        """Fetch an embedding artifact and materialize it into an EmbeddingSlice.

        Returns ``None`` if no completed artifact of ``asset_type`` exists.

        Loading strategy:

        - ``storage_type == 'filesystem'`` and the path ends in ``.npy``:
          loaded with ``numpy.load`` (memmap when ``limit`` is set and the
          file is large; otherwise in-memory).
        - Otherwise: returns an ``EmbeddingSlice`` with ``embeddings=None``
          and ``osm_ids=()`` — the caller falls back to ``handle.storage_path``
          (e.g. a TSV the operator parses itself, or a pgvector query).
        """
        handle = self.get_artifact_handle(asset_type, snapshot_date=snapshot_date)
        if handle is None:
            return None

        embeddings, osm_ids = self._load_embeddings(handle, limit=limit)
        # Map PipelineAsset.AssetType -> latent space label.
        latent_space = self._latent_space_for(handle.asset_type)
        return EmbeddingSlice(
            handle=handle,
            osm_ids=tuple(osm_ids),
            embeddings=embeddings,
            latent_space=latent_space,
        )

    def get_temporal_series(
        self, key: Tuple[str, int], snapshots: Iterable[str],
    ) -> "EmbeddingSlice":
        """Materialize a snapshot-indexed series from cross-shard queries.

        Cross-shard fan-out (v3 §11.3). Out of scope for the initial
        implementation; raised explicitly so callers know it is missing
        rather than silently getting an empty result.
        """
        raise NotImplementedError(
            "get_temporal_series cross-shard fan-out is not yet implemented "
            "(see TEMPORAL_SHARDING_ARTIFACT_PLAN.md Phase 3+)."
        )

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _latent_space_for(asset_type: str) -> str:
        """Map a PipelineAsset.AssetType value to a latent-space label."""
        mapping = {
            "GV_TAGS_EMBEDDING": "semantic",
            "GV_NLE_EMBEDDING": "geographic",
            "SUBGRAPH_PICKLE": "topological",
            "COUNTRY_PICKLE": "topological",
            "WIKIDATA_CANDIDATES": "ontological",
            "IGEA_ALIGNMENT": "ontological",
            "SPATIAL_LINKS": "topological",
            "METRICS": "temporal",
            "PBF_FILE": "geographic",
        }
        return mapping.get(asset_type, "unknown")

    def _load_embeddings(self, handle: ArtifactHandle, limit: Optional[int] = None):
        """Load embeddings from disk or return (None, ()) when not loadable.

        Only ``.npy`` files are auto-loaded here. TSV / pgvector paths are
        left for the caller (operators know their own formats best).
        """
        if not _HAS_NUMPY:
            return None, ()
        path = handle.storage_path
        if not path or not path.endswith(".npy"):
            return None, ()
        try:
            import os
            if not os.path.exists(path):
                logger.warning("EmbeddingSlice: .npy path missing: %s", path)
                return None, ()
            if limit is not None:
                arr = np.load(path, mmap_mode="r")[: int(limit)]
            else:
                arr = np.load(path)
            # Embeddings-only file: synthesize sequential osm_ids.
            osm_ids = tuple(range(int(arr.shape[0])))
            return arr, osm_ids
        except Exception as exc:  # pragma: no cover - disk/format errors
            logger.warning("EmbeddingSlice load failed for %s: %s", path, exc)
            return None, ()
