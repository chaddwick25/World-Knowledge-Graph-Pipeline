"""
System Summary API — aggregation endpoint for the init dashboard.

Provides a comprehensive report of:
  - Planet file status
  - Storage (hot/cold) usage
  - Embeddings availability by continent
  - All configured paths (from verify_paths)
  - Pipeline run history
  - Preprocessing step status
"""

import logging
from pathlib import Path

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class SystemSummaryView(APIView):
    """GET /api/system/summary/ — comprehensive init report."""

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        data = {
            "planet": self._get_planet_info(),
            "storage": self._get_storage_info(),
            "embeddings": self._get_embeddings_info(),
            "paths": self._get_paths_info(),
            "pipeline_runs": self._get_pipeline_run_info(),
            "preprocessing_steps": self._get_preprocessing_steps(),
        }
        return Response(data)

    # ── Planet ──────────────────────────────────────────────────────────

    def _get_planet_info(self):
        from api.models import PbfFile

        planet = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.PLANET,
            status=PbfFile.PbfStatus.COMPLETED,
        ).first()

        if not planet:
            return {"available": False, "path": None, "size_gb": None}

        # Use actual file size as fallback
        size_bytes = planet.size_bytes
        if not size_bytes and planet.path:
            try:
                size_bytes = Path(planet.path).stat().st_size
            except OSError:
                pass

        return {
            "available": True,
            "path": planet.path,
            "size_gb": round(size_bytes / (1024 ** 3), 1) if size_bytes else 0,
        }

    # ── Storage ──────────────────────────────────────────────────────────

    def _get_storage_info(self):
        from django.conf import settings
        hot_path = Path(settings.HOT_STORAGE_PATH) if settings.HOT_STORAGE_PATH else Path(settings.BASE_DATA_DIR)
        cold_path = Path(settings.COLD_STORAGE_PATH) if settings.COLD_STORAGE_PATH else Path(settings.COLD_STORAGE_BASE_DIR)

        # Hot storage (SSD/NVME — working files)
        hot_info = self._path_summary(hot_path)
        cold_info = self._path_summary(cold_path)

        return {
            "hot": {
                "path": str(hot_path),
                "exists": hot_path.exists(),
                "file_count": hot_info["file_count"],
                "size_gb": hot_info["size_gb"],
                "contents": "configs used in the pipeline, models, extractions",
            },
            "cold": {
                "path": str(cold_path),
                "exists": cold_path.exists(),
                "file_count": cold_info["file_count"],
                "size_gb": cold_info["size_gb"],
                "contents": "Pre-trained GeoVectors embeddings (TSV) needed to produce the pickle at runtime, Tensors(pt) used at Runtime,",
            },
        }

    # ── Embeddings ──────────────────────────────────────────────────────

    def _get_embeddings_info(self):
        from orchestration.models import CountryPipelineProfile, PipelineRun
        from extraction.models import OSMWikiDataHierarchy

        total = CountryPipelineProfile.objects.count()
        with_embeddings = CountryPipelineProfile.objects.filter(has_embeddings=True).count()

        # GeoVectors TSV count
        with_tsv = sum(
            1
            for p in CountryPipelineProfile.objects.iterator()
            if p.country_relations_payload
            and p.country_relations_payload.get("geovectors_location_tsv")
        )

        # By continent using OSMWikiDataHierarchy parent_slug
        hierarchy_parents = {}
        for h in OSMWikiDataHierarchy.objects.values("slug", "parent_slug"):
            slug = h["slug"]
            parent = h["parent_slug"] or "other"
            if parent not in hierarchy_parents:
                hierarchy_parents[parent] = {"total": 0, "with_emb": 0, "pipelines": 0}

        for p in CountryPipelineProfile.objects.all().only("embedding_slug", "has_embeddings"):
            slug = p.embedding_slug
            if not slug:
                # Try resolving slug from canonical_slug or iso
                slug = p.canonical_slug or p.iso2.lower()
            parent = None
            for h in OSMWikiDataHierarchy.objects.filter(slug=slug).values("parent_slug"):
                parent = h["parent_slug"]
                break
            parent = parent or "other"
            if parent not in hierarchy_parents:
                hierarchy_parents[parent] = {"total": 0, "with_emb": 0, "pipelines": 0}
            hierarchy_parents[parent]["total"] += 1
            if p.has_embeddings:
                hierarchy_parents[parent]["with_emb"] += 1

        # Count completed pipelines per continent
        for p in PipelineRun.objects.filter(status="COMPLETED").values(
            "country_code", "country_name"
        ):
            # Resolve continent for this country
            iso = p["country_code"]
            profile = CountryPipelineProfile.objects.filter(iso2__iexact=iso).first()
            if profile:
                slug = profile.embedding_slug or profile.canonical_slug or iso.lower()
                parent = None
                for h in OSMWikiDataHierarchy.objects.filter(slug=slug).values("parent_slug"):
                    parent = h["parent_slug"]
                    break
                parent = parent or "other"
                if parent in hierarchy_parents:
                    hierarchy_parents[parent]["pipelines"] += 1

        by_continent = [
            {
                "name": parent,
                "total": info["total"],
                "with_embeddings": info["with_emb"],
                "pipelines_completed": info["pipelines"],
            }
            for parent, info in sorted(hierarchy_parents.items())
        ]

        return {
            "total_countries": total,
            "with_embeddings": with_embeddings,
            "with_tsv": with_tsv,
            "by_continent": by_continent,
        }

    # ── Paths (verify_paths-style) ──────────────────────────────────────

    def _get_paths_info(self):
        path_configs = [
            ("PLANET_OSM_FILE_PATH", settings.PLANET_OSM_FILE_PATH),
            ("POLYGON_FILES_DIR", settings.POLYGON_FILES_DIR),
            ("PBF_CACHE_DIR", settings.PBF_CACHE_DIR),
            ("ASSET_BUNDLES_DIR", settings.ASSET_BUNDLES_DIR),
            ("COLD_STORAGE_BASE_DIR", settings.COLD_STORAGE_BASE_DIR),
            ("CORPUS_DIR", settings.CORPUS_DIR),
            ("OSM_WIKIDATA_EXTRACTIONS_DIR", settings.OSM_WIKIDATA_EXTRACTIONS_DIR),
            ("OSM_CONTINENTS_OUTPUT_DIR", settings.OSM_CONTINENTS_OUTPUT_DIR),
            ("EMBEDDINGS_ROOT", settings.EMBEDDINGS_ROOT),
            ("FASTTEXT_MODEL_PATH", settings.FASTTEXT_MODEL_PATH),
            ("OSMIUM_BINARY_PATH", getattr(settings, "OSMIUM_BINARY_PATH", None)),
        ]

        results = []
        for key, path_val in path_configs:
            if not path_val:
                results.append({"key": key, "path": None, "exists": False})
                continue
            p = Path(path_val)
            info = {
                "key": key,
                "path": str(p),
                "exists": p.exists(),
            }
            if p.exists():
                if p.is_file():
                    info["size_mb"] = round(p.stat().st_size / (1024 * 1024), 1)
                elif p.is_dir():
                    file_count = sum(1 for _ in p.rglob("*") if _.is_file())
                    info["file_count"] = file_count
                    if not info.get("size_mb"):
                        info["size_mb"] = round(
                            sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                            / (1024 * 1024),
                            1,
                        )
                # Check if executable
                if p.is_file() and p.stat().st_mode & 0o111:
                    info["is_executable"] = True
            results.append(info)

        return results

    # ── Pipeline runs ───────────────────────────────────────────────────

    def _get_pipeline_run_info(self):
        from orchestration.models import PipelineRun

        completed = PipelineRun.objects.filter(
            status__in=["COMPLETED", "SUCCESS"]
        ).count()
        failed = PipelineRun.objects.filter(status="FAILED").count()

        recent = list(
            PipelineRun.objects.filter(pipeline_type="worldkg_v2")
            .exclude(status__in=["PENDING", "RUNNING"])
            .order_by("-completed_at")[:10]
            .values("country_code", "country_name", "status", "completed_at", "configuration")
        )

        # Extract snapshot_date from config JSON for display
        for r in recent:
            config = r.get("configuration", {}) or {}
            if isinstance(config, dict):
                r["snapshot"] = config.get("snapshot_date", "unknown")
            else:
                r["snapshot"] = "unknown"

        return {
            "completed": completed,
            "failed": failed,
            "recent": recent,
        }

    # ── Preprocessing steps ─────────────────────────────────────────────

    def _get_preprocessing_steps(self):
        """Check status of each pre-build command.

        These are idempotent — we check if the output data exists rather
        than tracking a separate status flag.
        """
        from orchestration.models import CountryPipelineProfile, PipelineRun

        # enrich_worldkg_classes: check if any entities have worldkg_class in Redis
        # or if the ontology TTL was loaded (we check by seeing if any profiles
        # have country_relations_payload populated)
        enrich_ok = CountryPipelineProfile.objects.filter(
            country_relations_payload__isnull=False
        ).exists()

        # prebuild_structure: CountryPipelineProfile records exist
        structure_ok = CountryPipelineProfile.objects.count() > 0

        # prebuild_country_paths: any path fields populated
        paths_ok = (
            CountryPipelineProfile.objects.exclude(
                country_relations_payload__isnull=True
            ).count()
            > 0
        )

        # prebuild_subgraphs: any SubgraphProfile records exist
        from orchestration.models import SubgraphProfile
        subgraph_ok = SubgraphProfile.objects.count() > 0

        # prebuild_wikidata_ids: OSMWikiDataHierarchy has Q-IDs
        from extraction.models import OSMWikiDataHierarchy
        wikidata_ok = (
            OSMWikiDataHierarchy.objects.exclude(wikidata_id__isnull=True)
            .exclude(wikidata_id="")
            .count()
            > 0
        )

        return {
            "enrich_worldkg_classes": {
                "status": "completed" if enrich_ok else "not_run",
                "description": "WorldKG ontology loaded and class mapping ready",
            },
            "prebuild_structure": {
                "status": "completed" if structure_ok else "not_run",
                "countries": CountryPipelineProfile.objects.count(),
                "description": "CountryPipelineProfile records populated",
            },
            "prebuild_country_paths": {
                "status": "completed" if paths_ok else "not_run",
                "countries": CountryPipelineProfile.objects.count(),
                "description": "Filesystem paths resolved for all countries",
            },
            "prebuild_subgraphs": {
                "status": "completed" if subgraph_ok else "not_run",
                "count": SubgraphProfile.objects.count(),
                "description": "Geofabrik-based subgraph records created",
            },
            "prebuild_wikidata_ids": {
                "status": "completed" if wikidata_ok else "not_run",
                "count": OSMWikiDataHierarchy.objects.exclude(
                    wikidata_id__isnull=True
                )
                .exclude(wikidata_id="")
                .count(),
                "description": "Wikidata Q-IDs backfilled into hierarchy",
            },
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    def _path_summary(self, path: Path) -> dict:
        if not path.exists():
            return {"file_count": 0, "size_gb": 0}
        files = list(path.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        return {
            "file_count": file_count,
            "size_gb": round(total_size / (1024 ** 3), 1),
        }
