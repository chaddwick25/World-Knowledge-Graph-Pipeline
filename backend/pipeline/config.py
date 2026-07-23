"""
CountryConfig — Centralized configuration for a single pipeline run.
Every pipeline step reads from this dataclass instead of reaching into
settings.py, environment variables, or making implicit assumptions.
Design principles:
1. **Single source of truth** — All per-country parameters in one place.
2. **Explicit over implicit** — has_pretrained_nle, has_subgraphs, etc.
   are explicit booleans that drive code paths.
3. **DB-backed defaults + settings.py overrides** — CountryPipelineProfile
   provides the country-specific config; settings.py provides global defaults.
4. **Serialisable as dict** — Passed through Celery as a plain dict; each
   task reconstructs it via `CountryConfig.from_dict()`.
"""

# TODO: Refactor this entire file to be more modular and easier to understand
import dataclasses
import logging
from pathlib import Path
from typing import List, Optional
from uuid import uuid4
from django.conf import settings

logger = logging.getLogger(__name__)
# ──────────────────────────────────────────────────────────────────────────
# Path resolution helper
# ──────────────────────────────────────────────────────────────────────────
def _resolve_country_pbf_path(
    path_service, continent_norm: str, candidate_slugs: List[str],
) -> Optional[str]:
    """Resolve the country PBF path by trying candidate slugs.

    Tries each slug in *candidate_slugs* and returns the first path
    whose parent directory exists on the filesystem.  Falls back to
    the first candidate if none exist.
    """
    for candidate in candidate_slugs:
        try:
            if hasattr(path_service, "get_country_pbf_path"):
                pbf_path = path_service.get_country_pbf_path(continent_norm, candidate)
                if pbf_path.parent.exists():
                    return str(pbf_path)
        except Exception:
            continue
    # Fallback: return the first candidate path anyway
    if hasattr(path_service, "get_country_pbf_path"):
        return str(path_service.get_country_pbf_path(continent_norm, candidate_slugs[0]))
    return None

# ──────────────────────────────────────────────────────────────────────────
# SubgraphConfig
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class SubgraphConfig:
    """Configuration for a single subgraph (admin region / city).

    Subgraphs allow large countries (USA, Canada, Russia) to be processed
    on commercial hardware by splitting the country into smaller regions
    that each fit in GPU memory.
    """

    name: str
    slug: str
    osm_relation_id: Optional[int] = None
    wikidata_qid: Optional[str] = None
    admin_level: Optional[int] = None

    # Paths (resolved at config-build time)
    poly_path: Optional[str] = None
    pbf_path: Optional[str] = None
    pickle_path: Optional[str] = None

    # Bounding box (for filtering)
    bbox_min_lon: Optional[float] = None
    bbox_min_lat: Optional[float] = None
    bbox_max_lon: Optional[float] = None
    bbox_max_lat: Optional[float] = None

    @classmethod
    def from_db(cls, profile: "SubgraphProfile") -> "SubgraphConfig":
        """Build from a SubgraphProfile DB row."""
        return cls(
            name=profile.name,
            slug=profile.slug,
            osm_relation_id=profile.osm_relation_id,
            wikidata_qid=profile.wikidata_id,
            admin_level=profile.admin_level,
            poly_path=profile.subgraph_poly_path,
            pbf_path=profile.subgraph_pbf_path,
            pickle_path=profile.subgraph_pickle_path,
            bbox_min_lon=profile.bbox_min_lon,
            bbox_min_lat=profile.bbox_min_lat,
            bbox_max_lon=profile.bbox_max_lon,
            bbox_max_lat=profile.bbox_max_lat,
        )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubgraphConfig":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})



# TODO: Refactor, look into how configs are handled in Celery making a folder and having a config file for each country
# Also look into moving _resolve_country_pbf_path and other relevant functions to the helper module
# ──────────────────────────────────────────────────────────────────────────
# CountryConfig
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class CountryConfig:
    """Everything the pipeline needs to know about a country.

    This is the **contract** that every pipeline step reads from.
    Steps receive a serialised dict; they reconstruct this object via
    ``CountryConfig.from_dict()``.

    Fields are grouped by concern:
    - **Identity**: Who is this country?
    - **Alignment hub**: Wikidata QID + OSM relation ID (the PK-FK bridge)
    - **Pre-trained model availability**: Natural filter for encoding path
    - **Subgraphs**: For commercial hardware support
    - **FastText / GV-Tags**: Semantic embedding config
    - **USLP (link prediction)**: Scoring thresholds and gating params
    - **DeepWalk (GV-NLE)**: Training hyperparameters
    - **Entropy gate**: Minimum semantic diversity to proceed
    - **Snapshot**: Which temporal snapshot to use
    - **Pipeline state**: Celery tracking
    """

    # ── Identity ──────────────────────────────────────────────────────────
    iso: str                                # "MZ"
    name: str                               # "Mozambique"
    slug: str                               # "mozambique"
    continent: str                          # "africa"

    # ── Alignment hub (Wikidata ↔ OSM ↔ Geofabrik) ───────────────────────
    wikidata_qid: Optional[str] = None         # "Q1029"
    osm_relation_id: Optional[int] = None      # 195267

    # ── GeoVectors pre-trained model availability (NATURAL FILTER) ────────
    has_pretrained_nle: bool = False        # ← Drives encoding code path
    has_pretrained_tags: bool = False       # ← If True, use GeoVectors TSV tags
    geovectors_tsv_path: Optional[str] = None  # Path to locations.tsv.gz
    pickle_path: Optional[str] = None          # Path to wdw.pickle
    embedding_slug: Optional[str] = None       # Directory slug under EMBEDDINGS_ROOT

    # ── Subgraphs (for commercial hardware) ──────────────────────────────
    has_subgraphs: bool = False             # ← Drives parallel processing
    subgraphs: List[SubgraphConfig] = dataclasses.field(default_factory=list)

    # ── FastText / GV-Tags ────────────────────────────────────────────────
    fasttext_model_path: Optional[str] = None  # cc.en.300.bin or tuned variant
    # TODO: load the parameters for these configs externally
    # ── USLP (Link Prediction — the gating layer) ────────────────────────
    uslp_threshold: float = 0.7
    uslp_top_k: int = 50
    uslp_limit: int = 200000
    uslp_max_heads: int = 50000
    uslp_use_gpu: bool = True
    uslp_gpu_device: str = "cuda:0"

    # ── DeepWalk / GV-NLE Training ────────────────────────────────────────
    deepwalk_k: int = 50
    deepwalk_embedding_dim: int = 100
    deepwalk_walk_length: int = 80
    deepwalk_num_walks: int = 10
    deepwalk_workers: int = 26
    deepwalk_use_gpu: bool = True
    deepwalk_gpu_device: str = "cuda:0"
    deepwalk_buffer_deg: float = 0.45

    # ── Entropy gate ──────────────────────────────────────────────────────
    min_entropy: float = 1.5
    min_entropy_delta: float = 0.2

    # ── Snapshot ──────────────────────────────────────────────────────────
    snapshot_date: str = "2025_12_31"
    snapshot_pbf_path: Optional[str] = None  # Full path to snapshot PBF (serialised through Celery)
    poly_path: Optional[str] = None          # Full path to .poly file (serialised through Celery)

    # ── Pipeline state (Celery tracking) ─────────────────────────────────
    pipeline_run_id: Optional[str] = None       # UUID string

    # ── Debug / override flags ────────────────────────────────────────────
    skip_enrich: bool = False                   # Skip WorldKG enrichment step

    # ── Derived paths (computed, not serialised) ─────────────────────────
    _pbf_path: Optional[str] = dataclasses.field(default=None, repr=False)

    # ──────────────────────────────────────────────────────────────────────
    # Construction helpers
    # ──────────────────────────────────────────────────────────────────────

    @classmethod
    def from_db(cls, iso: str, snapshot_date: Optional[str] = None) -> "CountryConfig":
        """Build from CountryPipelineProfile + SubgraphProfile + settings.

        This is the PRIMARY construction path. It resolves:
        - The country profile from the DB
        - Subgraph profiles (if any)
        - Paths from regional_path_service
        - Global defaults from settings.py

        Args:
            iso: ISO 3166-1 alpha-2 code (e.g., "MZ", "GB", "CA") or a
                 synthetic code for non-sovereign territories (e.g., "WL", "SC").
            snapshot_date: Override snapshot date (YYYY_MM_DD). If None, uses
                           settings.SINGLE_SNAPSHOT_DATE.

        Returns:
            Fully-populated CountryConfig
        """
        from orchestration.models import CountryPipelineProfile, SubgraphProfile
        from extraction.services.regional_path_service import (
            regional_path_service,
            normalize_country_slug,
        )
        from extraction.models import OSMWikiDataHierarchy

        # ── 1. Resolve country profile ──────────────────────────────────
        profile = (
            CountryPipelineProfile.objects.filter(
                iso2__iexact=iso,
            )
            .select_related("continent_profile", "planet_snapshot")
            .first()
        )

        if not profile:
            # Try iso3 fallback
            profile = (
                CountryPipelineProfile.objects.filter(iso3__iexact=iso)
                .select_related("continent_profile", "planet_snapshot")
                .first()
            )

        if not profile:
            # Non-sovereign synthetic ISO handling (Wales, Scotland, England, etc.)
            # These territories have no CountryPipelineProfile but have GeoVectors
            # TSV files on disk and EligibleCountry entries.
            from extraction.services.non_sovereign_territories import (
                is_non_sovereign_synthetic_iso,
                synthetic_iso_to_info,
            )
            from orchestration.models import EligibleCountry

            if is_non_sovereign_synthetic_iso(iso):
                iso_upper = iso.upper()
                info = synthetic_iso_to_info(iso_upper)
                if info:
                    # Create a synthetic profile on-the-fly so the rest of
                    # from_db() can proceed normally.
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
                    # Last resort: try EligibleCountry
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
                        logger.info(
                            "Created synthetic CountryPipelineProfile from EligibleCountry for %s (ISO=%s)",
                            eligible.country_name, iso_upper,
                        )

            if not profile:
                raise ValueError(
                    f"No CountryPipelineProfile found for ISO '{iso}'. "
                    f"Run `initialize_planet` first or check that the country "
                    f"has embeddings in cold storage."
                )

        # ── 1b. Enrich from OSMWikiDataHierarchy (populated from
        #       country_relations.json via initialize_planet) ─────────
        # CountryPipelineProfile may be missing osm_relation_id, continent,
        # wikidata_id. Fill these in from the DB hierarchy so all pre-processing
        # and pipeline steps can work.
        # Skip for synthetic profiles (non-sovereign territories without hierarchy).
        from extraction.services.non_sovereign_territories import (
            is_non_sovereign_synthetic_iso,
        )
        hierarchy = None
        if not is_non_sovereign_synthetic_iso(iso):
            hierarchy = OSMWikiDataHierarchy.objects.filter(
                slug=profile.embedding_slug or profile.canonical_slug,
            ).first()
            if not hierarchy:
                hierarchy = OSMWikiDataHierarchy.objects.filter(
                    name__iexact=profile.canonical_name,
                ).first()

        # For synthetic (non-sovereign) ISOs, populate osm_relation_id and
        # wikidata_qid from the registry, since they have no OSMWikiDataHierarchy entry.
        from extraction.services.non_sovereign_territories import (
            synthetic_iso_to_info as _synthetic_iso_to_info,
        )

        enriched_relation_id = profile.osm_relation_id
        enriched_continent   = profile.continent_name or ""
        enriched_wikidata_id = profile.wikidata_id

        # If still missing and this is a synthetic ISO, pull from registry
        if is_non_sovereign_synthetic_iso(iso):
            syn_info = _synthetic_iso_to_info(iso)
            if syn_info:
                if not enriched_relation_id:
                    enriched_relation_id = syn_info.get("osm_relation_id")
                if not enriched_continent:
                    enriched_continent = syn_info.get("continent", "")
                if not enriched_wikidata_id:
                    enriched_wikidata_id = syn_info.get("wikidata_qid")

        if hierarchy:
            if not enriched_relation_id and hierarchy.osm_relation_id:
                enriched_relation_id = hierarchy.osm_relation_id
            if not enriched_continent and hierarchy.parent_slug:
                enriched_continent = hierarchy.parent_slug
            if not enriched_wikidata_id and hierarchy.wikidata_id:
                enriched_wikidata_id = hierarchy.wikidata_id

        # ── 2. Resolve subgraphs ────────────────────────────────────────
        # Try filesystem discovery FIRST — it's always authoritative since
        # subgraph directories on disk reflect the actual available data.
        # DB SubgraphProfile records may be stale, incomplete, or missing.
        has_subgraphs = False
        subgraphs = []
        try:
            from extraction.services.subgraph_list_service import build_subgraph_list
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

        # Fallback: use DB SubgraphProfile records if filesystem discovery
        # returned nothing. Prefer poly-backed subgraphs, then PBF-backed.
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

        # Build a candidate list of slugs to try when resolving filesystem paths.
        # The canonical slug (from DB/Geofabrik) may differ from the override slug
        # (from overrides.json), and the actual snapshot may exist under either one.
        # For example, Ireland's canonical slug is 'ireland-and-northern-ireland'
        # but the override maps IE → 'ireland'. Try both (and the normalized variant).
        # Prioritize the override slug since it's the authoritative source for path construction.
        candidate_slugs = []
        if iso:
            from extraction.services.country_override_service import get_country_slug as _get_override_slug
            override_slug = _get_override_slug(iso, country_norm)
            if override_slug and override_slug not in candidate_slugs:
                candidate_slugs.append(override_slug)
        candidate_slugs.append(country_norm)
        # Also try the normalize_country_slug version of the canonical slug
        normed = normalize_country_slug(country_norm)
        if normed not in candidate_slugs:
            candidate_slugs.append(normed)

        # Snapshot PBF path (try single-snapshot first, fall back to monthly)
        snapshot_pbf_path = None
        try:
            from django.conf import settings as dj_settings
            snap_date = snapshot_date or getattr(dj_settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")
            for candidate in candidate_slugs:
                ss_path = regional_path_service.get_single_snapshot_pbf_path(
                    cont_norm, candidate, snap_date,
                )
                if ss_path.exists():
                    snapshot_pbf_path = str(ss_path)
                    break
                # Fallback: monthly-snapshot path (YYYY_MM_DD.pbf)
                ms_path = regional_path_service.get_snapshot_pbf_path(
                    cont_norm, candidate, 2025, 12, 31,
                )
                if ms_path.exists():
                    snapshot_pbf_path = str(ms_path)
                    break
            if not snapshot_pbf_path:
                # Store the first-candidate path anyway (will be generated during preprocess)
                first_ss = regional_path_service.get_single_snapshot_pbf_path(
                    cont_norm, candidate_slugs[0], snap_date,
                )
                snapshot_pbf_path = str(first_ss)
        except Exception:
            pass

        # Pickle path — try all candidate slugs
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

        # GeoVectors TSV path (from profile's country_relations_payload
        # or OSMWikiDataHierarchy pbf_url)
        geovectors_tsv_path = None
        rel_payload = profile.country_relations_payload or {}
        tsv_rel = rel_payload.get("geovectors_location_tsv")
        if not tsv_rel and hierarchy:
            # Fallback: build path from hierarchy data
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

        # ── 5. Assemble ─────────────────────────────────────────────────
        # _poly_path is no longer needed — poly files are generated on-demand
        # by the TemporalOrchestratorService phase 3 from the snapshot + relation ID.
        return cls(
            iso=iso.upper(),
            name=profile.canonical_name,
            slug=country_norm,
            continent=continent,
            wikidata_qid=enriched_wikidata_id,
            osm_relation_id=enriched_relation_id,
            has_pretrained_nle=profile.has_embeddings,
            has_pretrained_tags=bool(geovectors_tsv_path),
            geovectors_tsv_path=geovectors_tsv_path,
            pickle_path=pickle_path,
            embedding_slug=profile.embedding_slug or profile.embedding_root_path,
            has_subgraphs=has_subgraphs,
            subgraphs=subgraphs,
            fasttext_model_path=getattr(settings, "FASTTEXT_MODEL_PATH", None),
            uslp_threshold=float(getattr(settings, "USLP_THRESHOLD", 0.7)),
            uslp_top_k=int(getattr(settings, "USLP_TOP_K", 50)),
            uslp_limit=int(getattr(settings, "USLP_LIMIT", 200000)),
            uslp_max_heads=int(getattr(settings, "USLP_MAX_HEADS", 50000)),
            uslp_use_gpu=str(getattr(settings, "USLP_USE_GPU", "true")).lower() in ("true", "1", "yes"),
            uslp_gpu_device=str(getattr(settings, "USLP_GPU_DEVICE", "cuda:0")),
            deepwalk_k=int(getattr(settings, "DEEPWALK_K", 50)),
            deepwalk_workers=int(getattr(settings, "DEEPWALK_WORKERS", 26)),
            deepwalk_use_gpu=str(getattr(settings, "DEEPWALK_USE_GPU", "true")).lower() in ("true", "1", "yes"),
            deepwalk_gpu_device=str(getattr(settings, "DEEPWALK_GPU_DEVICE", "cuda:0")),
            min_entropy=float(getattr(settings, "MIN_PREFLIGHT_SHANNON_ENTROPY", 1.5)),
            min_entropy_delta=float(getattr(settings, "MIN_PREFLIGHT_ENTROPY_DELTA", 0.2)),
            snapshot_date=snapshot_date or str(getattr(settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")),
            pipeline_run_id=str(uuid4()),
            snapshot_pbf_path=snapshot_pbf_path,
            poly_path=None,
            _pbf_path=_resolve_country_pbf_path(
                regional_path_service, cont_norm, candidate_slugs,
            ),
        )

    # ──────────────────────────────────────────────────────────────────────
    # Serialisation (for passing through Celery)
    # ──────────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise to a JSON-safe dict for Celery messaging."""
        result = {}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name == "subgraphs":
                result["subgraphs"] = [sg.to_dict() for sg in value]
            elif field.name.startswith("_"):
                continue  # Skip private fields
            else:
                result[field.name] = value
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "CountryConfig":
        """Reconstruct from a dict (received from Celery)."""
        # Rebuild subgraphs without mutating the input dict. The config dict is
        # passed through multiple Celery steps; using pop() here would remove
        # the "subgraphs" key for downstream steps, causing has_subgraphs=True
        # but an empty subgraphs list by the time we reach Step 5.
        field_names = {f.name for f in dataclasses.fields(cls)}
        subgraph_dicts = d.get("subgraphs", []) or []
        cfg = cls(**{
            k: v for k, v in d.items()
            if k in field_names
        })
        cfg.subgraphs = [SubgraphConfig.from_dict(sg) for sg in subgraph_dicts]
        return cfg

    # ──────────────────────────────────────────────────────────────────────
    # Convenience properties
    # ──────────────────────────────────────────────────────────────────────

    @property
    def pbf_path(self) -> Optional[str]:
        return self._pbf_path

    @property
    def pickle_dir(self) -> Optional[str]:
        """Parent directory of the pickle file (needed by NLEModel)."""
        if self.pickle_path:
            return str(Path(self.pickle_path).parent)
        return None

    def __repr__(self) -> str:
        return (
            f"CountryConfig(iso={self.iso}, name={self.name}, "
            f"has_pretrained_nle={self.has_pretrained_nle}, "
            f"has_subgraphs={self.has_subgraphs}, "
            f"subgraphs={len(self.subgraphs)}, "
            f"pipeline_run_id={self.pipeline_run_id})"
        )
