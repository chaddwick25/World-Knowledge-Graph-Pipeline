"""Country pipeline envelope types.

Split out of ``pipeline/envelopes.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN): StorageTarget, CountryIdentity,
CountryPaths, CountryRunState and CountryEnvelope.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import uuid4

from django.conf import settings

from pipeline.config import SubgraphConfig, _resolve_country_pbf_path
from pipeline.hyperparams import ModelHyperparams

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# StorageTarget (frozen — the sharding primitive)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class StorageTarget:
    """Which shard to query — a typed address, not a connection."""
    shard_key: str
    backend: str = "postgres_vectors"
    db_alias: str = "default"
    path: Optional[str] = None
    table: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────
# CountryIdentity (frozen)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class CountryIdentity:
    """Who is this country — frozen at from_db time."""
    iso: str
    name: str
    slug: str
    continent: str
    wikidata_qid: Optional[str] = None
    osm_relation_id: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────────
# CountryPaths (frozen)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class CountryPaths:
    """Resolved filesystem paths — frozen at from_db time."""
    geovectors_tsv_path: Optional[str] = None
    pickle_path: Optional[str] = None
    embedding_slug: Optional[str] = None
    snapshot_pbf_path: Optional[str] = None
    poly_path: Optional[str] = None
    pbf_path: Optional[str] = None

    @property
    def pickle_dir(self) -> Optional[str]:
        if self.pickle_path:
            return str(Path(self.pickle_path).parent)
        return None


# ──────────────────────────────────────────────────────────────────────────
# CountryRunState (mutable — the ONLY mutable part)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class CountryRunState:
    """Mutable run state — set during the run, passed task→task."""
    pipeline_run_id: Optional[str] = None
    snapshot_date: str = "2025_12_31"
    skip_enrich: bool = False
    has_pretrained_nle: bool = False
    has_pretrained_tags: bool = False
    has_subgraphs: bool = False
    igea_accepted: int = 0
    igea_stats: Optional[dict] = None
    # Transient (NOT serialized by to_dict): artifact descriptors produced by
    # the task body, consumed by the @pipeline_step on_success hook to emit
    # PipelineAsset rows. Each descriptor is a dict with keys like
    # asset_type, asset_name, stage_name, storage_path, record_count, ...
    pending_assets: List[dict] = dataclasses.field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# CountryEnvelope (frozen except state)
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class CountryEnvelope:
    """The message passed between country-pipeline Celery tasks."""
    identity: CountryIdentity
    paths: CountryPaths
    hyperparams: ModelHyperparams
    subgraphs: Tuple[SubgraphConfig, ...]
    state: CountryRunState
    storage: StorageTarget

    # ── Convenience accessors (match legacy CountryConfig field names) ───
    @property
    def iso(self) -> str:
        return self.identity.iso

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def slug(self) -> str:
        return self.identity.slug

    @property
    def continent(self) -> str:
        return self.identity.continent

    @property
    def wikidata_qid(self) -> Optional[str]:
        return self.identity.wikidata_qid

    @property
    def osm_relation_id(self) -> Optional[int]:
        return self.identity.osm_relation_id

    @property
    def pipeline_run_id(self) -> Optional[str]:
        return self.state.pipeline_run_id

    @property
    def snapshot_date(self) -> str:
        return self.state.snapshot_date

    @property
    def skip_enrich(self) -> bool:
        return self.state.skip_enrich

    @property
    def has_pretrained_nle(self) -> bool:
        return self.state.has_pretrained_nle

    @property
    def has_pretrained_tags(self) -> bool:
        return self.state.has_pretrained_tags

    @property
    def has_subgraphs(self) -> bool:
        return self.state.has_subgraphs

    @property
    def igea_stats(self) -> Optional[dict]:
        return self.state.igea_stats

    @property
    def geovectors_tsv_path(self) -> Optional[str]:
        return self.paths.geovectors_tsv_path

    @property
    def pickle_path(self) -> Optional[str]:
        return self.paths.pickle_path

    @property
    def pickle_dir(self) -> Optional[str]:
        return self.paths.pickle_dir

    @property
    def embedding_slug(self) -> Optional[str]:
        return self.paths.embedding_slug

    @property
    def snapshot_pbf_path(self) -> Optional[str]:
        return self.paths.snapshot_pbf_path

    @property
    def poly_path(self) -> Optional[str]:
        return self.paths.poly_path

    @property
    def pbf_path(self) -> Optional[str]:
        return self.paths.pbf_path

    # Hyperparam accessors (match legacy CountryConfig field names)
    @property
    def uslp_threshold(self) -> float:
        return self.hyperparams.uslp_threshold

    @property
    def uslp_top_k(self) -> int:
        return self.hyperparams.uslp_top_k

    @property
    def uslp_limit(self) -> int:
        return self.hyperparams.uslp_limit

    @property
    def uslp_max_heads(self) -> int:
        return self.hyperparams.uslp_max_heads

    @property
    def uslp_use_gpu(self) -> bool:
        return self.hyperparams.uslp_use_gpu

    @property
    def uslp_gpu_device(self) -> str:
        return self.hyperparams.uslp_gpu_device

    @property
    def deepwalk_k(self) -> int:
        return self.hyperparams.deepwalk_k

    @property
    def deepwalk_embedding_dim(self) -> int:
        return self.hyperparams.deepwalk_embedding_dim

    @property
    def deepwalk_walk_length(self) -> int:
        return self.hyperparams.deepwalk_walk_length

    @property
    def deepwalk_num_walks(self) -> int:
        return self.hyperparams.deepwalk_num_walks

    @property
    def deepwalk_workers(self) -> int:
        return self.hyperparams.deepwalk_workers

    @property
    def deepwalk_use_gpu(self) -> bool:
        return self.hyperparams.deepwalk_use_gpu

    @property
    def deepwalk_gpu_device(self) -> str:
        return self.hyperparams.deepwalk_gpu_device

    @property
    def deepwalk_buffer_deg(self) -> float:
        return self.hyperparams.deepwalk_buffer_deg

    @property
    def min_entropy(self) -> float:
        return self.hyperparams.min_entropy

    @property
    def min_entropy_delta(self) -> float:
        return self.hyperparams.min_entropy_delta

    @property
    def fasttext_model_path(self) -> Optional[str]:
        return self.hyperparams.fasttext_model_path

    # ── State mutation ───────────────────────────────────────────────────
    def with_state(self, **kw) -> "CountryEnvelope":
        """Return a copy with updated run state."""
        new_state = dataclasses.replace(self.state, **kw)
        return dataclasses.replace(self, state=new_state)

    # ── Serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """Serialize for Celery.

        Hyperparams are included as an override dict so that runtime
        overrides (e.g. skip_entropy_gate → min_entropy=0.0) survive
        the trip through Celery. If no overrides were applied, the
        dict is empty and from_dict() will load from YAML as usual.
        """
        yaml_hp = ModelHyperparams.load_from_yaml()
        hp_overrides = {}
        if self.hyperparams != yaml_hp:
            hp_overrides = dataclasses.asdict(self.hyperparams)
        return {
            # Identity
            "iso": self.identity.iso,
            "name": self.identity.name,
            "slug": self.identity.slug,
            "continent": self.identity.continent,
            "wikidata_qid": self.identity.wikidata_qid,
            "osm_relation_id": self.identity.osm_relation_id,
            # Paths
            "geovectors_tsv_path": self.paths.geovectors_tsv_path,
            "pickle_path": self.paths.pickle_path,
            "embedding_slug": self.paths.embedding_slug,
            "snapshot_pbf_path": self.paths.snapshot_pbf_path,
            "poly_path": self.paths.poly_path,
            # State
            "pipeline_run_id": self.state.pipeline_run_id,
            "snapshot_date": self.state.snapshot_date,
            "skip_enrich": self.state.skip_enrich,
            "has_pretrained_nle": self.state.has_pretrained_nle,
            "has_pretrained_tags": self.state.has_pretrained_tags,
            "has_subgraphs": self.state.has_subgraphs,
            "igea_accepted": self.state.igea_accepted,
            "igea_stats": self.state.igea_stats,
            # Subgraphs
            "subgraphs": [sg.to_dict() for sg in self.subgraphs],
            # Storage
            "storage_shard_key": self.storage.shard_key,
            "storage_db_alias": self.storage.db_alias,
            # Hyperparam overrides (only present if mutated at runtime)
            "hyperparam_overrides": hp_overrides,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CountryEnvelope":
        """Reconstruct from a Celery dict. Re-attaches frozen hyperparams.

        If ``hyperparam_overrides`` is present in the dict (non-empty),
        those values take precedence over the YAML defaults. This allows
        runtime overrides like skip_entropy_gate to survive serialization.
        """
        hyperparams = ModelHyperparams.load_from_yaml()
        hp_overrides = d.get("hyperparam_overrides") or {}
        if hp_overrides:
            hyperparams = dataclasses.replace(hyperparams, **hp_overrides)

        identity = CountryIdentity(
            iso=d.get("iso", ""),
            name=d.get("name", ""),
            slug=d.get("slug", ""),
            continent=d.get("continent", ""),
            wikidata_qid=d.get("wikidata_qid"),
            osm_relation_id=d.get("osm_relation_id"),
        )
        paths = CountryPaths(
            geovectors_tsv_path=d.get("geovectors_tsv_path"),
            pickle_path=d.get("pickle_path"),
            embedding_slug=d.get("embedding_slug"),
            snapshot_pbf_path=d.get("snapshot_pbf_path"),
            poly_path=d.get("poly_path"),
            pbf_path=d.get("pbf_path") or d.get("_pbf_path"),
        )
        state = CountryRunState(
            pipeline_run_id=d.get("pipeline_run_id"),
            snapshot_date=d.get("snapshot_date", "2025_12_31"),
            skip_enrich=d.get("skip_enrich", False),
            has_pretrained_nle=d.get("has_pretrained_nle", False),
            has_pretrained_tags=d.get("has_pretrained_tags", False),
            has_subgraphs=d.get("has_subgraphs", False),
            igea_accepted=d.get("igea_accepted", 0),
            igea_stats=d.get("igea_stats"),
        )
        subgraph_dicts = d.get("subgraphs", []) or []
        subgraphs = tuple(SubgraphConfig.from_dict(sg) for sg in subgraph_dicts)
        storage = StorageTarget(
            shard_key=d.get("storage_shard_key", d.get("continent", "")),
            db_alias=d.get("storage_db_alias", "default"),
        )
        return cls(
            identity=identity,
            paths=paths,
            hyperparams=hyperparams,
            subgraphs=subgraphs,
            state=state,
            storage=storage,
        )

    @classmethod
    def from_db(cls, iso: str, snapshot_date: Optional[str] = None) -> "CountryEnvelope":
        """Build from CountryPipelineProfile + SubgraphProfile + settings.

        Ports the logic from the legacy CountryConfig.from_db(), splitting the result
        into identity/paths/hyperparams/state/storage.
        """
        from core.models import CountryPipelineProfile, SubgraphProfile
        from core.services.snapshot.regional_path_service import (
            regional_path_service,
            normalize_country_slug,
        )
        from core.models import OSMWikiDataHierarchy

        # ── 1. Resolve country profile ──────────────────────────────────
        profile = (
            CountryPipelineProfile.objects.filter(
                iso2__iexact=iso,
            )
            .select_related("continent_profile", "planet_snapshot")
            .first()
        )

        if not profile:
            profile = (
                CountryPipelineProfile.objects.filter(iso3__iexact=iso)
                .select_related("continent_profile", "planet_snapshot")
                .first()
            )

        if not profile:
            # Non-sovereign synthetic ISO handling
            from core.services.planet_init.non_sovereign_territories import (
                is_non_sovereign_synthetic_iso,
                synthetic_iso_to_info,
            )
            from core.models import EligibleCountry

            if is_non_sovereign_synthetic_iso(iso):
                iso_upper = iso.upper()
                info = synthetic_iso_to_info(iso_upper)
                if info:
                    profile, _ = CountryPipelineProfile.objects.get_or_create(
                        iso2=iso_upper,
                        defaults={
                            'canonical_name': info['name'],
                            'canonical_slug': info['slug'],
                            'embedding_slug': info['slug'],
                            'embedding_root_path': f"{info['continent']}/{info['slug']}",
                            'continent_name': info['continent'],
                            'has_subgraphs': False,
                        },
                    )
                    logger.info(
                        "Created synthetic CountryPipelineProfile for %s (ISO=%s)",
                        info['name'], iso_upper,
                    )
                else:
                    eligible = EligibleCountry.objects.filter(
                        iso_code__iexact=iso,
                    ).first()
                    if eligible:
                        profile, _ = CountryPipelineProfile.objects.get_or_create(
                            iso2=iso_upper,
                            defaults={
                                'canonical_name': eligible.country_name,
                                'canonical_slug': eligible.country_name.lower().replace(' ', '_'),
                                'embedding_slug': eligible.country_name.lower().replace(' ', '_'),
                                'embedding_root_path': f"{eligible.continent}/{eligible.country_name.lower().replace(' ', '_')}",
                                'continent_name': eligible.continent,
                                'has_subgraphs': False,
                            },
                        )

            if not profile:
                raise ValueError(
                    f"No CountryPipelineProfile found for ISO '{iso}'. "
                    f"Run `initialize_planet` first or check that the country "
                    f"has embeddings in cold storage."
                )

        # ── 1b. Enrich from OSMWikiDataHierarchy ────────────────────────
        from core.services.planet_init.non_sovereign_territories import (
            is_non_sovereign_synthetic_iso as _is_synth,
            synthetic_iso_to_info as _synth_info,
        )

        hierarchy = None
        if not _is_synth(iso):
            hierarchy = OSMWikiDataHierarchy.objects.filter(
                slug=profile.embedding_slug or profile.canonical_slug,
            ).first()
            if not hierarchy:
                hierarchy = OSMWikiDataHierarchy.objects.filter(
                    name__iexact=profile.canonical_name,
                ).first()

        enriched_relation_id = profile.osm_relation_id
        enriched_continent = profile.continent_name or ""
        enriched_wikidata_id = profile.wikidata_id

        if _is_synth(iso):
            info = _synth_info(iso.upper())
            if info:
                if not enriched_relation_id:
                    enriched_relation_id = info.get("osm_relation_id")
                if not enriched_wikidata_id:
                    enriched_wikidata_id = info.get("wikidata_qid")
                if not enriched_continent:
                    enriched_continent = info.get("continent", "")

        if not enriched_relation_id and hierarchy:
            enriched_relation_id = hierarchy.osm_relation_id
        if not enriched_wikidata_id and hierarchy:
            enriched_wikidata_id = hierarchy.wikidata_id
        if not enriched_continent and hierarchy:
            enriched_continent = hierarchy.parent_slug or ""
        if not enriched_continent and profile.continent_profile:
            enriched_continent = profile.continent_profile.slug

        # ── 2. Subgraphs ────────────────────────────────────────────────
        # Try filesystem discovery FIRST — it's always authoritative since
        # subgraph directories on disk reflect the actual available data.
        # DB SubgraphProfile records may be stale, incomplete, or missing.
        has_subgraphs = False
        subgraphs: List[SubgraphConfig] = []

        # Filesystem discovery
        try:
            from core.services.snapshot.subgraph_list_service import build_subgraph_list
            fs_subgraphs = build_subgraph_list(
                country_name=profile.canonical_name,
                auto_all=True,
            )
            if fs_subgraphs:
                has_subgraphs = True
                subgraphs = [
                    SubgraphConfig(
                        name=sg.get("name", ""),
                        slug=sg.get("slug", ""),
                        poly_path=sg.get("poly_path"),
                    ) for sg in fs_subgraphs
                ]
                logger.info(
                    "Discovered %d subgraphs from filesystem for %s (ISO=%s)",
                    len(subgraphs), profile.canonical_name, iso,
                )
        except Exception as exc:
            logger.warning(
                "Filesystem subgraph discovery failed for %s: %s",
                iso, exc,
            )

        # Enrich filesystem-discovered subgraphs with QID / bbox / relation_id
        # from SubgraphProfile DB records (populated by the pipeline's
        # sync_subgraph_profiles helper).
        if has_subgraphs and subgraphs:
            import dataclasses as _dc
            sg_by_slug = {sg.slug: sg for sg in subgraphs}
            db_profiles = SubgraphProfile.objects.filter(
                country_profile=profile,
            ).values(
                "slug", "wikidata_id", "osm_relation_id", "admin_level",
                "bbox_min_lon", "bbox_min_lat", "bbox_max_lon", "bbox_max_lat",
                "subgraph_pbf_path", "subgraph_poly_path", "subgraph_pickle_path",
            )
            enriched_count = 0
            for db_sg in db_profiles:
                sg = sg_by_slug.get(db_sg["slug"])
                if not sg:
                    continue
                # Build replacement dict — only fill in missing fields
                repl = {}
                if db_sg["wikidata_id"] and not sg.wikidata_qid:
                    repl["wikidata_qid"] = db_sg["wikidata_id"]
                    enriched_count += 1
                if db_sg["osm_relation_id"] and not sg.osm_relation_id:
                    repl["osm_relation_id"] = db_sg["osm_relation_id"]
                if db_sg["admin_level"] is not None and sg.admin_level is None:
                    repl["admin_level"] = db_sg["admin_level"]
                if db_sg["bbox_min_lon"] is not None and sg.bbox_min_lon is None:
                    repl["bbox_min_lon"] = db_sg["bbox_min_lon"]
                    repl["bbox_min_lat"] = db_sg["bbox_min_lat"]
                    repl["bbox_max_lon"] = db_sg["bbox_max_lon"]
                    repl["bbox_max_lat"] = db_sg["bbox_max_lat"]
                if db_sg["subgraph_pbf_path"] and not sg.pbf_path:
                    repl["pbf_path"] = db_sg["subgraph_pbf_path"]
                if db_sg["subgraph_poly_path"] and not sg.poly_path:
                    repl["poly_path"] = db_sg["subgraph_poly_path"]
                if db_sg["subgraph_pickle_path"] and not sg.pickle_path:
                    repl["pickle_path"] = db_sg["subgraph_pickle_path"]
                if repl:
                    sg_by_slug[db_sg["slug"]] = _dc.replace(sg, **repl)
            subgraphs = list(sg_by_slug.values())
            if enriched_count:
                logger.info(
                    "Enriched %d/%d subgraphs with Wikidata QIDs from DB for %s",
                    enriched_count, len(subgraphs), iso,
                )

        # DB fallback
        if not has_subgraphs:
            subgraph_profiles = SubgraphProfile.objects.filter(
                country_profile=profile,
                has_subgraph_poly=True,
            )
            if not subgraph_profiles.exists():
                subgraph_profiles = SubgraphProfile.objects.filter(
                    country_profile=profile,
                    has_subgraph_pbf=True,
                )
            if subgraph_profiles.exists():
                has_subgraphs = True
                subgraphs = [SubgraphConfig.from_db(sg) for sg in subgraph_profiles]
                logger.info(
                    "Fell back to %d DB subgraph profiles for %s (ISO=%s)",
                    len(subgraphs), profile.canonical_name, iso,
                )

        # ── 3. Resolve continent ────────────────────────────────────────
        continent = enriched_continent
        if not continent and profile.continent_profile:
            continent = profile.continent_profile.slug

        # ── 4. Resolve paths ────────────────────────────────────────────
        cont_norm = normalize_country_slug(continent)
        country_norm = profile.canonical_slug or normalize_country_slug(profile.canonical_name)

        candidate_slugs = []
        if iso:
            from core.services.snapshot.country_override_service import get_country_slug as _get_override_slug
            override_slug = _get_override_slug(iso, country_norm)
            if override_slug and override_slug not in candidate_slugs:
                candidate_slugs.append(override_slug)
        candidate_slugs.append(country_norm)
        normed = normalize_country_slug(country_norm)
        if normed not in candidate_slugs:
            candidate_slugs.append(normed)

        # Snapshot PBF path
        snapshot_pbf_path = None
        try:
            snap_date = snapshot_date or getattr(settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")
            for candidate in candidate_slugs:
                ss_path = regional_path_service.get_single_snapshot_pbf_path(
                    cont_norm, candidate, snap_date,
                )
                if ss_path.exists():
                    snapshot_pbf_path = str(ss_path)
                    break
                ms_path = regional_path_service.get_snapshot_pbf_path(
                    cont_norm, candidate, 2025, 12, 31,
                )
                if ms_path.exists():
                    snapshot_pbf_path = str(ms_path)
                    break
            if not snapshot_pbf_path:
                first_ss = regional_path_service.get_single_snapshot_pbf_path(
                    cont_norm, candidate_slugs[0], snap_date,
                )
                snapshot_pbf_path = str(first_ss)
        except Exception:
            pass

        # Pickle path
        pickle_path = None
        try:
            for candidate in candidate_slugs:
                pickle_dir = regional_path_service.get_pickle_dir(cont_norm, candidate)
                pickle_candidate = pickle_dir / "wdw.pickle"
                if pickle_candidate.exists():
                    pickle_path = str(pickle_candidate)
                    break
        except Exception:
            pass

        # GeoVectors TSV path
        geovectors_tsv_path = None
        rel_payload = profile.country_relations_payload or {}
        tsv_rel = rel_payload.get("geovectors_location_tsv")
        if not tsv_rel and hierarchy:
            if hierarchy.pbf_url:
                tsv_rel = hierarchy.pbf_url.replace(
                    "-latest.osm.pbf", "-location.tsv.gz"
                )
        if tsv_rel:
            tsv_path = Path(tsv_rel)
            if not tsv_path.is_absolute():
                tsv_path = Path(settings.EMBEDDINGS_ROOT) / tsv_rel
            if tsv_path.exists():
                geovectors_tsv_path = str(tsv_path)

        # Country PBF path
        pbf_path = _resolve_country_pbf_path(
            regional_path_service, cont_norm, candidate_slugs,
        )

        # ── 4. Assemble ─────────────────────────────────────────────────
        hyperparams = ModelHyperparams.load_from_yaml()

        identity = CountryIdentity(
            iso=iso.upper(),
            name=profile.canonical_name,
            slug=country_norm,
            continent=continent,
            wikidata_qid=enriched_wikidata_id,
            osm_relation_id=enriched_relation_id,
        )
        paths = CountryPaths(
            geovectors_tsv_path=geovectors_tsv_path,
            pickle_path=pickle_path,
            embedding_slug=profile.embedding_slug or profile.embedding_root_path,
            snapshot_pbf_path=snapshot_pbf_path,
            poly_path=None,
            pbf_path=pbf_path,
        )
        state = CountryRunState(
            pipeline_run_id=str(uuid4()),
            snapshot_date=snapshot_date or str(getattr(settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")),
            skip_enrich=False,
            has_pretrained_nle=profile.has_embeddings,
            has_pretrained_tags=bool(geovectors_tsv_path),
            has_subgraphs=has_subgraphs,
            igea_accepted=0,
        )
        storage = StorageTarget(
            shard_key=f"{continent}/{country_norm}",
            db_alias="default",
        )

        return cls(
            identity=identity,
            paths=paths,
            hyperparams=hyperparams,
            subgraphs=tuple(subgraphs),
            state=state,
            storage=storage,
        )

    def __repr__(self) -> str:
        return (
            f"CountryEnvelope(iso={self.identity.iso}, name={self.identity.name}, "
            f"has_subgraphs={self.state.has_subgraphs}, "
            f"subgraphs={len(self.subgraphs)}, "
            f"pipeline_run_id={self.state.pipeline_run_id})"
        )
