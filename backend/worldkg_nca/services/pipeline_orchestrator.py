"""
WorldKG Unified Country Pipeline Service

⚠️  DEPRECATED  ⚠️
This thread-based pipeline service is superseded by the Celery Canvas
pipeline. Use ``pipeline.canvas.run_worldkg_pipeline()`` instead.

  1. Celery version: ``poetry run python manage.py run_pipeline <ISO>``
  2. API version: ``POST /api/worldkg-pipeline-v2/start/``
  3. Step 0 (planet init) and Step 0.5 (continent init) available via
     ``pipeline.canvas.run_planet_initialization()``.

This service is retained for backward compatibility only and calls
``warnings.warn(DeprecationWarning)`` on instantiation. It will be
removed in Phase 2 of the pipeline consolidation effort.
"""

import logging
import os
import subprocess
import threading
import warnings
from datetime import date, timedelta
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
# TODO: Remove this import when removing this file
from worldkg_nca.services.wikidata_service import ISO_BBOX_FALLBACK
from worldkg_nca.services.projection_weight_service import ProjectionWeightService
from worldkg_nca.services.precompute_link_candidates_service import run_precompute_for_region
from extraction.models import OSMWikiDataHierarchy
from orchestration.models import PipelineRun, PipelineAsset
from extraction.services.regional_path_service import normalize_country_slug

logger = logging.getLogger(__name__)

# Build ISO code lookup from the comprehensive ISO_BBOX_FALLBACK dict (180+ countries)
COUNTRY_NAME_TO_ISO = {code: code for code in ISO_BBOX_FALLBACK.keys()}

TOTAL_STEPS = 15


#  TODO: Audit then remove this entire file
class WorldKGPipelineService:
    """
    ⚠️  DEPRECATED  ⚠️  Use ``pipeline.canvas.run_worldkg_pipeline()`` instead.

    Runs all 8 WorldKG pipeline steps in a background thread.
    Pushes step-level updates to the Django Channels group for the session.
    """

    def __init__(self, session_id, country_name, pbf_path=None, skip_preflight=False):
        warnings.warn(
            "WorldKGPipelineService is deprecated. "
            "Use pipeline.canvas.run_worldkg_pipeline() via Celery instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.session_id = str(session_id)
        self.country_name = country_name
        self.skip_preflight = skip_preflight
        # Determine ISO code: check if already 2-letter code, else derive from name
        if len(country_name) == 2 and country_name.upper() in ISO_BBOX_FALLBACK:
            self.iso_code = country_name.upper()
        else:
            # Try common name variations: "monaco" → "MC", "el_salvador" → "SV"
            self.iso_code = self._resolve_iso_code(country_name)
        self.pbf_path = pbf_path
        self.channel_layer = get_channel_layer()
        self.group_name = f'pipeline_{self.session_id}'
        self._step_num = 0
        
        # Load OSMWikiDataHierarchy config for this country
        self.osm_wikidata = self._load_osm_wikidata_config()
        
        # Create PipelineRun for tracking
        self.pipeline_run = self._create_pipeline_run()

    @staticmethod
    def _resolve_iso_code(name):
        """
        Resolve country name to ISO 3166-1 alpha-2 code.

        QID-FIRST resolution:
          1. Check OSMWikiDataHierarchy DB (Wikidata QID as primary key).
          2. Fallback to slug/name mappings.

        Handles variations like "monaco", "el_salvador", "British Virgin Islands".
        """
        if not name:
            return ''

        # Fast path: if it already looks like a 2-letter ISO code, return it.
        # This avoids hitting the DB or name_map for entities whose addr:country
        # tag is already an ISO code (e.g. "CV" for Cape Verde).
        cleaned = name.strip().upper()
        if len(cleaned) == 2 and cleaned.isalpha():
            return cleaned

        # QID-first: try OSMWikiDataHierarchy DB lookup
        try:
            # Also try lookup by wikidata_id if input looks like a QID
            qid_filter = None
            qid_clean = name.strip().upper()
            if qid_clean.startswith('Q') and len(qid_clean) > 1 and qid_clean[1:].isdigit():
                qid_filter = OSMWikiDataHierarchy.objects.filter(
                    admin_level=2,
                    wikidata_id__iexact=qid_clean,
                ).first()
            hierarchy = qid_filter or OSMWikiDataHierarchy.objects.filter(
                admin_level=2,
                name__iexact=name.replace('_', ' '),
            ).first()
            if hierarchy:
                # Prefer iso2 from CountryPipelineProfile over QID
                from orchestration.models import CountryPipelineProfile
                profile = CountryPipelineProfile.objects.filter(
                    osm_relation_id=hierarchy.osm_relation_id
                ).first()
                if profile and profile.iso2:
                    return profile.iso2.upper()
                # If no profile yet, look up by wikidata_id in country_relations
                from extraction.services.osm_wikidata_resolver import get_country_relations_dict
                relations = get_country_relations_dict()
                wg_uri = hierarchy.wikidata_uri or ''
                wid = hierarchy.wikidata_id or ''
                for iso_code, data in relations.items():
                    # Check multiple places for Wikidata ID:
                    # 1. Key itself might be a QID (e.g., 'Q242' -> 'Belize')
                    # 2. wikidata_id field
                    # 3. wkg_uri field (contains .../Q242)
                    key_is_qid = iso_code.upper() == wid
                    field_wid = (data.get('wikidata_id') or '').upper()
                    uri_wid = (data.get('wkg_uri') or '').split('/')[-1].upper()
                    if key_is_qid or field_wid == wid or uri_wid == wid:
                        # Found it! Now derive a real ISO code
                        iso = iso_code.upper()
                        if len(iso) != 2 or not iso.isalpha():
                            # ISO key isn't a real ISO code; try osm_relation_id tables
                            for k2, d2 in relations.items():
                                if len(k2) == 2 and k2.isalpha():
                                    if d2.get('relation_id') == data.get('relation_id'):
                                        iso = k2.upper()
                                        break
                        # Also create CountryPipelineProfile on-the-fly
                        try:
                            from orchestration.models import CountryPipelineProfile
                            if not CountryPipelineProfile.objects.filter(iso2=iso).exists():
                                CountryPipelineProfile.objects.get_or_create(
                                    canonical_slug=data.get('slug', iso.lower()),
                                    defaults={
                                        'iso2': iso if len(iso) == 2 and iso.isalpha() else None,
                                        'canonical_name': data.get('name', iso),
                                        'embedding_slug': data.get('slug', iso.lower()),
                                        'embedding_root_path': data.get('slug', iso.lower()),
                                        'osm_relation_id': data.get('relation_id'),
                                        'wikidata_id': hierarchy.wikidata_id,
                                        'continent_name': data.get('continent_name', ''),
                                    }
                                )
                        except Exception:
                            pass
                        return iso
                return hierarchy.wikidata_id.upper()
        except Exception:
            pass

        name = name.strip()

        # Slug-based lookup against country_relations.json (Geofabrik metadata).
        # This handles callers that pass a Geofabrik-style slug such as
        # "ireland-and-northern-ireland" or "Ireland_and_northern_ireland".
        try:
            from extraction.services.osm_wikidata_resolver import get_country_relations_dict

            relations = get_country_relations_dict()
            slug = normalize_country_slug(name)
            for iso_code, data in relations.items():
                data_slug = (data or {}).get("slug") or ""
                if not data_slug:
                    continue
                # Normalise stored slug as well (hyphens/underscores) for robust match.
                if normalize_country_slug(data_slug) == slug:
                    # Only accept real 2-letter ISO codes here; skip QID-style keys.
                    if len(iso_code) == 2 and iso_code.isalpha():
                        return iso_code.upper()
        except Exception:
            # Best-effort enhancement; fall back to legacy mappings if anything fails.
            pass

        # Non-sovereign synthetic ISO check (Wales, Scotland, England, etc.)
        # These territories have GeoVectors TSVs but no official ISO code.
        try:
            from extraction.services.non_sovereign_territories import (
                resolve_non_sovereign_iso,
            )
            synthetic_iso = resolve_non_sovereign_iso(name)
            if synthetic_iso:
                return synthetic_iso
        except Exception:
            pass

        # Common name mappings (lowercase slug → ISO code)
        name_map = {
            'monaco': 'MC', 'martinique': 'MQ', 'guadeloupe': 'GP',
            'british_virgin_islands': 'VG', 'us_virgin_islands': 'VI',
            'turks_and_caicos': 'TC', 'turks_and_caicos_islands': 'TC',
            'cayman_islands': 'KY',
            'bermuda': 'BM', 'greenland': 'GL', 'faroe_islands': 'FO',
            'isle_of_man': 'IM', 'jersey': 'JE', 'guernsey': 'GG',
            'gibraltar': 'GI', 'falkland_islands': 'FK',
            'tanzania': 'TZ', 'jamaica': 'JM', 'honduras': 'HN',
            'nicaragua': 'NI', 'panama': 'PA', 'belize': 'BZ',
        }
        slug = normalize_country_slug(name)
        if slug in name_map:
            return name_map[slug]
        
        # Try title case normalization: "el_salvador" → "El Salvador"
        # Then fuzzy match against ISO_BBOX_FALLBACK keys
        normalized = name.replace('_', ' ').title()
        if normalized in ['El Salvador', 'Costa Rica', 'New Zealand', 'South Africa',
                          'South Korea', 'North Korea', 'Saudi Arabia', 'United Kingdom',
                          'United States', 'Puerto Rico', 'Sri Lanka']:
            # These are in ISO_BBOX_FALLBACK, map to their codes
            code_map = {
                'El Salvador': 'SV', 'Costa Rica': 'CR', 'New Zealand': 'NZ',
                'South Africa': 'ZA', 'South Korea': 'KR', 'North Korea': 'KP',
                'Saudi Arabia': 'SA', 'United Kingdom': 'GB', 'United States': 'US',
                'Puerto Rico': 'PR', 'Sri Lanka': 'LK',
            }
            return code_map.get(normalized, '')
        
        # Fallback: check if it's already in ISO_BBOX_FALLBACK as a key
        if name.upper() in ISO_BBOX_FALLBACK:
            return name.upper()
        
        logger.warning(f'Could not resolve ISO code for: {name}')
        return ''
    
    def _load_osm_wikidata_config(self):
        """Load OSMWikiDataHierarchy configuration for this country.

        QID-first resolution: tries wikidata_id (ISO/QID), then slug match.
        """
        try:
            # QID-first: try wikidata_id (may be ISO code or Wikidata QID)
            if self.iso_code:
                hierarchy = OSMWikiDataHierarchy.objects.filter(
                    wikidata_id__icontains=self.iso_code,
                    admin_level=2
                ).first()
                if hierarchy:
                    logger.info(
                        f'Loaded OSMWikiData config for {self.country_name} '
                        f'via wikidata_id={self.iso_code}'
                    )
                    return hierarchy
            
            # Fallback to name match
            country_slug = self.country_name.lower().strip().replace(' ', '_').replace('-', '_')
            hierarchy = OSMWikiDataHierarchy.objects.filter(
                slug__icontains=country_slug,
                admin_level=2
            ).first()
            if hierarchy:
                logger.info(f'Loaded OSMWikiData config for {self.country_name} via slug match')
                return hierarchy
            
            logger.warning(f'No OSMWikiData config found for {self.country_name}, using default policy')
            return None
        except Exception as exc:
            logger.warning(f'Failed to load OSMWikiData config: {exc}')
            return None
    
    def _create_pipeline_run(self):
        """Create PipelineRun for tracking this pipeline execution."""
        try:
            from orchestration.models import ProcessingSession
            session = ProcessingSession.objects.filter(id=self.session_id).first()
            
            pipeline_run = PipelineRun.objects.create(
                osm_wikidata_hierarchy=self.osm_wikidata,
                country_code=self.iso_code or 'N/A',
                country_name=self.country_name,
                pipeline_type='worldkg_unified',
                status=PipelineRun.PipelineStatus.PENDING,
                configuration={
                    'pbf_path': self.pbf_path,
                    'iso_code': self.iso_code,
                    'osm_wikidata_config': self.osm_wikidata.processing_policy if self.osm_wikidata else None
                },
                processing_session=session
            )
            logger.info(f'Created PipelineRun: {pipeline_run.id}')
            return pipeline_run
        except Exception as exc:
            logger.warning(f'Failed to create PipelineRun: {exc}')
            return None

    def _create_asset(self, asset_type, asset_path, parent_asset=None, stage_name=None, metadata=None):
        """
        Create a PipelineAsset record for tracking output files.
        
        Args:
            asset_type: Type of asset (PBF, PICKLE, EMBEDDING, POLY, HIERARCHY, METRICS)
            asset_path: Filesystem path to the asset
            parent_asset: Parent PipelineAsset for lineage tracking
            stage_name: Name of the stage that created this asset
            metadata: Additional metadata dictionary
        
        Returns:
            PipelineAsset instance or None if creation failed
        """
        if not self.pipeline_run:
            return None
        
        try:
            import hashlib
            from pathlib import Path
            
            path = Path(asset_path)
            if not path.exists():
                logger.warning(f'Asset path does not exist: {asset_path}')
                return None
            
            # Calculate file hash (SHA-256)
            file_hash = None
            try:
                with open(path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
            except Exception as e:
                logger.warning(f'Failed to calculate hash for {asset_path}: {e}')
            
            # Get file size
            size_bytes = path.stat().st_size
            
            asset = PipelineAsset.objects.create(
                pipeline_run=self.pipeline_run,
                asset_type=asset_type,
                asset_name=path.name,
                storage_path=str(path),
                parent_asset=parent_asset,
                stage_name=stage_name,
                metadata=metadata or {},
                file_size_bytes=size_bytes
            )
            logger.info(f'Created PipelineAsset: {asset_type} - {asset_path}')
            return asset
        except Exception as exc:
            logger.warning(f'Failed to create PipelineAsset for {asset_path}: {exc}')
            return None

    def run_in_background(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def _push(self, msg_type, **kwargs):
        try:
            async_to_sync(self.channel_layer.group_send)(
                self.group_name,
                {'type': msg_type, **kwargs}
            )
        except Exception as exc:
            logger.warning('Channel push failed: %s', exc)

    def _step_start(self, name, message):
        self._step_num += 1
        self._push('step_update',
                   step=self._step_num, total=TOTAL_STEPS,
                   name=name, status='in_progress', message=message, pct=0)
        logger.info('[%d/%d] %s — %s', self._step_num, TOTAL_STEPS, name, message)
        
        # Track stage start in PipelineRun
        if self.pipeline_run:
            self.pipeline_run.start_stage(name)

    def _step_done(self, name, message, pct=100):
        self._push('step_update',
                   step=self._step_num, total=TOTAL_STEPS,
                   name=name, status='completed', message=message, pct=pct)
        
        # Track stage completion in PipelineRun
        if self.pipeline_run:
            self.pipeline_run.complete_stage(name, {'message': message})

    def _step_fail(self, name, message):
        self._push('step_update',
                   step=self._step_num, total=TOTAL_STEPS,
                   name=name, status='failed', message=message, pct=0)
        
        # Track stage failure in PipelineRun
        if self.pipeline_run:
            self.pipeline_run.mark_failed(message)

    def _log(self, message):
        self._push('log', message=message)
        logger.info(message)

    def _get_cache_path(self):
        """Unified cache path for Wikidata candidates."""
        cache_dir = Path(settings.BASE_DIR.parent) / 'data' / 'wikidata_cache'
        cache_dir.mkdir(parents=True, exist_ok=True)
        country_slug = self.country_name.lower().strip().replace(' ', '_').replace('-', '_')
        return str(cache_dir / f'{country_slug}_candidates.json')

    def _run(self):
        try:
            self._step1_verify_redis()
            self._step2_load_ontology()
            region_pbf = self._step3_extract_region_pbf()

            # Pre-flight entropy gate (unless explicitly skipped)
            if not self.skip_preflight:
                preflight = self._preflight_entropy_check(region_pbf)
                if not preflight['proceed']:
                    self._push('pipeline_complete',
                               session_id=self.session_id,
                               status='skipped',
                               reason=preflight['reason'])
                    self._update_session_status('SKIPPED', error=preflight['reason'])
                    if self.pipeline_run:
                        self.pipeline_run.mark_completed({
                            'status': 'skipped',
                            'reason': preflight['reason'],
                            'preflight_entropy': preflight['shannon_entropy'],
                        })
                    return
            else:
                self._log('Pre-flight entropy check skipped (--skip-preflight flag)')

            self._step4_monthly_snapshots(region_pbf)
            self._step5_embed_osm_entities(region_pbf)
            self._step5b_enrich_worldkg()
            self._step6_harvest_wikidata()
            self._step7_run_igea()
            self._step8_predict_triples()
            self._step9_train_gv_nle()
            self._step11_compute_static_embeddings()
            self._step10_validate_links()
            self._step12_learn_projection_weights()

            self._step13_precompute_link_candidates()

            self._push('pipeline_complete',
                       session_id=self.session_id, status='completed')
            self._update_session_status('COMPLETED')
            
            # Mark PipelineRun as completed
            if self.pipeline_run:
                self.pipeline_run.mark_completed({'total_steps': TOTAL_STEPS})

        except Exception as exc:
            logger.exception('WorldKG pipeline failed at step %d', self._step_num)
            self._push('pipeline_complete',
                       session_id=self.session_id,
                       status='failed',
                       error=str(exc))
            self._update_session_status('FAILED', error=str(exc))

    def _update_session_status(self, status, error=None):
        try:
            from orchestration.models import ProcessingSession
            s = ProcessingSession.objects.filter(id=self.session_id).first()
            if s:
                s.status = status
                s.completed_at = timezone.now()
                if error:
                    s.results = {'error': error}
                s.save()
        except Exception as exc:
            logger.warning('Failed to update session status: %s', exc)

    def _step1_verify_redis(self):
        self._step_start('verify_redis', 'Checking Redis connection (ontology cache)...')
        import redis as redis_lib
        try:
            r = redis_lib.Redis(host='localhost', port=6379, db=0)
            r.ping()
            self._step_done('verify_redis', 'Redis is up on port 6379')
        except Exception:
            self._log('Redis not responding — attempting to start via Docker...')
            subprocess.run(
                ['docker', 'run', '-d', '--name', 'redis-worldkg',
                 '-p', '6379:6379', '--restart', 'unless-stopped', 'redis:7-alpine'],
                capture_output=True
            )
            import time
            time.sleep(3)
            r = redis_lib.Redis(host='localhost', port=6379, db=0)
            r.ping()
            self._step_done('verify_redis', 'Redis started via Docker')

    def _step2_load_ontology(self):
        self._step_start('load_ontology', 'Loading WorldKG ontology into Redis...')
        # TODO: look into this, classes not visible in the frontend use the TTL file
        ontology_path = os.path.join(
            settings.BASE_DIR.parent, 'data', 'worldkg_ontology_sample.json'
        )
        if not Path(ontology_path).exists():
            raise FileNotFoundError(f'Ontology file not found: {ontology_path}')
        call_command('enrich_worldkg_classes', load_ontology=ontology_path)
        self._step_done('load_ontology', 'Loaded WorldKG ontology classes')

    def _step3_extract_region_pbf(self):
        self._step_start('extract_region_pbf',
                         f'Extracting region PBF for {self.country_name}...')
        if self.pbf_path and Path(self.pbf_path).exists():
            self._step_done('extract_region_pbf',
                            f'Using provided PBF: {self.pbf_path}')
            return self.pbf_path

        from api.models import PbfFile, RegionHierarchy, PolygonFile, Task
        from extraction.services.extraction_service import run_pbf_extraction

        country_slug = self.country_name.lower().strip().replace(' ', '_').replace('-', '_')

        existing = PbfFile.objects.filter(
            path__icontains=country_slug,
            pbf_file_type='REGION'
        ).order_by('-created_at').first()
        if existing and Path(existing.path).exists():
            self._log(f'Found existing region PBF: {existing.path}')
            self._step_done('extract_region_pbf', f'Using existing PBF: {existing.path}')
            return existing

        polygon_file = self._find_polygon_file(country_slug)
        continent_pbf = self._find_continent_pbf(country_slug)
        if not polygon_file or not continent_pbf:
            raise ValueError(
                f'Cannot find polygon file or continent PBF for {self.country_name}'
            )
        # TODO: look into this later
        # Use the canonical OSM_WIKIDATA_EXTRACTIONS_DIR as root for region extracts
        base_root = getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', '/tmp/eda_extractions')
        base_dir = Path(base_root)
        region_dir = base_dir / country_slug
        region_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(region_dir / f'{country_slug}.pbf')

        task = Task.objects.create(
            task_type=Task.TaskType.EXTRACT_PBF,
            parameters={
                'source_pbf_id': str(continent_pbf.id),
                'poly_file_path': polygon_file,
                'output_pbf_path': output_path,
                'cpu_core_id': 12,
            }
        )
        self._log(f'Running extraction for {self.country_name} (Task: {task.id})...')
        run_pbf_extraction(
            source_pbf_id=str(continent_pbf.id),
            poly_file_path=polygon_file,
            output_pbf_path=output_path,
            cpu_core_id=12,
            task_id=str(task.id),
        )
        
        # The extraction service now automatically registers the PbfFile record.
        region_pbf = PbfFile.objects.filter(path=output_path).first()
        if not region_pbf:
            self._log(f'Warning: PbfFile not found at {output_path} after extraction. Creating manually...')
            region_pbf = PbfFile.objects.create(
                path=output_path,
                pbf_file_type=PbfFile.PbfType.REGION,
                parent_pbf=continent_pbf,
                status=PbfFile.PbfStatus.COMPLETED
            )
        
        # Track region PBF as PipelineAsset
        self._create_asset(
            asset_type='PBF',
            asset_path=output_path,
            parent_asset=None,
            stage_name='extract_region_pbf',
            metadata={
                'pbf_type': 'REGION',
                'parent_pbf': str(continent_pbf.path) if continent_pbf else None,
                'iso_code': self.iso_code
            }
        )
        
        self._step_done('extract_region_pbf', f'Region PBF registered: {region_pbf.id}')
        return region_pbf

    def _preflight_entropy_check(self, region_pbf):
        """
        Pre-flight: run lightweight Step 4+5 on the LATEST month only,
        compute Shannon entropy, gate on absolute + delta thresholds.

        Returns dict with: proceed, shannon_entropy, unique_classes,
                           total_entities, threshold, delta, previous_entropy, reason
        """
        import calendar
        from datetime import datetime

        self._step_start('preflight_entropy',
                         'Pre-flight: verifying database schema and computing Shannon entropy...')

        # Verify snapshot_id column exists in SpatialTripletScore
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'igea_spatial_triplet_score'
                    AND column_name = 'snapshot_id'
                """)
                if not cursor.fetchone():
                    raise ValueError(
                        'snapshot_id column missing from igea_spatial_triplet_score table. '
                        'Please run: python manage.py migrate igea --database=vectors'
                    )
            self._log('Database schema verified: snapshot_id column exists')
        except Exception as e:
            self._log(f'Database schema check failed: {e}. Attempting to apply migration...')
            try:
                from django.core.management import call_command
                call_command('migrate', 'igea', '--database=vectors', verbosity=0)
                self._log('Migration applied successfully')
            except Exception as migrate_error:
                self._log(f'Migration failed: {migrate_error}. Aborting pipeline.')
                raise ValueError(
                    f'Cannot proceed without snapshot_id column. '
                    f'Migration error: {migrate_error}'
                )

        threshold = getattr(settings, 'MIN_PREFLIGHT_SHANNON_ENTROPY', 1.5)
        min_delta = getattr(settings, 'MIN_PREFLIGHT_ENTROPY_DELTA', 0.2)

        result = {
            'proceed': True,
            'shannon_entropy': 0.0,
            'unique_classes': 0,
            'total_entities': 0,
            'threshold': threshold,
            'delta': None,
            'previous_entropy': None,
            'reason': '',
        }

        # Detect latest month from temporal range
        if not region_pbf.min_timestamp or not region_pbf.max_timestamp:
            self._log('No temporal metadata on region PBF. Skipping pre-flight, proceeding.')
            self._step_done('preflight_entropy', 'Skipped (no temporal metadata)')
            return result

        max_date = region_pbf.max_timestamp
        year, month = max_date.year, max_date.month
        last_day = calendar.monthrange(year, month)[1]
        target_date = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"

        self._log(f'Pre-flight target: {year}-{month:02d} (latest month in range)')

        # Generate single monthly snapshot
        out_dir = Path(region_pbf.path).parent / 'snapshots' / 'preflight'
        out_dir.mkdir(exist_ok=True, parents=True)

        tmp_path = str(out_dir / f"tmp_{year}_{month:02d}.pbf")
        final_path = str(out_dir / f"snapshot_{year}_{month:02d}.pbf")

        osmium_bin = getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium')

        if not Path(final_path).exists():
            self._log(f'Generating single snapshot for {year}-{month:02d}...')
            try:
                subprocess.run([
                    osmium_bin, 'time-filter', region_pbf.path, target_date,
                    '-o', tmp_path, '--overwrite'
                ], check=True, capture_output=True)
                subprocess.run([
                    osmium_bin, 'cat', tmp_path, '-o', final_path, '-f', 'pbf', '--overwrite'
                ], check=True, capture_output=True)
                Path(tmp_path).unlink(missing_ok=True)
            except Exception as e:
                self._log(f'Pre-flight snapshot failed: {e}. Proceeding with pipeline.')
                self._step_done('preflight_entropy', 'Failed (snapshot error), proceeding')
                return result
        else:
            self._log(f'Pre-flight snapshot already exists: {final_path}')

        # Create TemporalSnapshot first (needed for snapshot-id linkage)


        from api.models import TemporalSnapshot, WorldKGClassFingerprint
        from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service

        preflight_snapshot, _ = TemporalSnapshot.objects.get_or_create(
            region=self.country_name,
            timestamp=datetime(year, month, last_day, 23, 59, 59),
            snapshot_interval='MONTHLY',
            defaults={
                'pbf_file': region_pbf,
                'file_path': final_path,
            }
        )

        # Run embedding on single snapshot (link entities to preflight snapshot)
        self._log('Running embedding on pre-flight snapshot...')
        try:
            call_command(
                'extract_osm_embeddings',
                pbf_file=final_path,
                snapshot_id=str(preflight_snapshot.id),
                chunk_size=20000,
                batch_size=5000,
                workers=4,
                websocket_group=self.group_name,
                total_entities=100000,
            )
        except Exception as e:
            self._log(f'Pre-flight embedding failed: {e}. Proceeding with pipeline.')
            self._step_done('preflight_entropy', 'Failed (embedding error), proceeding')
            return result

        # Enrich entities with WorldKG classes (lightweight Step 6)
        self._log('Enriching pre-flight entities with WorldKG classes...')
        try:
            call_command(
                'enrich_worldkg_classes',
                snapshot_id=str(preflight_snapshot.id),
                batch_size=1000,
                skip_enriched=True,
            )
        except Exception as e:
            self._log(f'Pre-flight enrichment failed: {e}. Proceeding with pipeline.')
            self._step_done('preflight_entropy', 'Failed (enrichment error), proceeding')
            return result

        # Compute fingerprint via drift service
        drift_service = get_worldkg_drift_service()
        fingerprint = drift_service.compute_fingerprint(preflight_snapshot)

        shannon = fingerprint.shannon_entropy
        result['shannon_entropy'] = shannon
        result['unique_classes'] = fingerprint.unique_classes
        result['total_entities'] = fingerprint.total_entities

        self._log(
            f'Pre-flight fingerprint: H={shannon:.3f}, '
            f'{fingerprint.unique_classes} classes, {fingerprint.total_entities} entities'
        )

        # Store snapshot_id in PipelineRun configuration for later retrieval
        if self.pipeline_run:
            self.pipeline_run.configuration['snapshot_id'] = str(preflight_snapshot.id)
            self.pipeline_run.save(update_fields=['configuration'])
            self._log(f'Stored snapshot_id={preflight_snapshot.id} in PipelineRun configuration')

        # Gate 1: Absolute entropy threshold
        if shannon < threshold:
            result['proceed'] = False
            result['reason'] = (
                f'Shannon entropy {shannon:.3f} below absolute threshold {threshold:.3f} '
                f'(region too semantically homogeneous)'
            )
            self._log(result['reason'])
            self._step_done('preflight_entropy', result['reason'])
            return result

        # Gate 2: Entropy delta vs previous fingerprint
        previous = WorldKGClassFingerprint.objects.filter(
            region=self.country_name
        ).exclude(
            snapshot=preflight_snapshot
        ).order_by('-snapshot__timestamp').first()

        if previous is not None:
            delta = abs(shannon - previous.shannon_entropy)
            result['delta'] = delta
            result['previous_entropy'] = previous.shannon_entropy

            self._log(
                f'Previous entropy: {previous.shannon_entropy:.3f} '
                f'(delta={delta:.3f}, threshold={min_delta:.3f})'
            )

            if delta < min_delta:
                result['proceed'] = False
                result['reason'] = (
                    f'Entropy delta {delta:.3f} below threshold {min_delta:.3f} '
                    f'(no meaningful semantic change since {previous.snapshot.timestamp.date()})'
                )
                self._log(result['reason'])
                self._step_done('preflight_entropy', result['reason'])
                return result
        else:
            self._log('No previous fingerprint found — first run, proceeding.')

        self._step_done('preflight_entropy',
                        f'H={shannon:.3f} — proceeding with full pipeline')
        return result

    def _step4_monthly_snapshots(self, region_pbf):
        self._step_start('monthly_snapshots',
                         'Generating monthly snapshots (last day of each month)...')
        
        from api.models import PbfFile
        import calendar
        from datetime import datetime
        
        # Get the region PBF asset as parent for snapshots
        region_asset = None
        if self.pipeline_run:
            region_asset = PipelineAsset.objects.filter(
                pipeline_run=self.pipeline_run,
                stage_name='extract_region_pbf',
                storage_path=str(region_pbf.path)
            ).first()
        
        try:
            # Detect temporal range from region PBF
            if not region_pbf.min_timestamp or not region_pbf.max_timestamp:
                self._log('Region PBF has no temporal metadata. Skipping snapshot generation.')
                self._step_done('monthly_snapshots', 'Skipped (no temporal metadata)')
                return
            
            min_year = region_pbf.min_timestamp.year
            max_year = region_pbf.max_timestamp.year
            
            self._log(f'Detected temporal range: {min_year} to {max_year}')
            
            out_dir = Path(region_pbf.path).parent / 'snapshots'
            out_dir.mkdir(exist_ok=True, parents=True)
            
            osmium_bin = getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium')
            snapshots_created = 0
            
            # Generate monthly snapshots for all months in the temporal range
            for year in range(min_year, max_year + 1):
                for month in range(1, 13):
                    # Get last day of month
                    last_day = calendar.monthrange(year, month)[1]
                    target_date = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"
                    
                    # Create year subdirectory
                    year_dir = out_dir / str(year)
                    year_dir.mkdir(exist_ok=True, parents=True)
                    
                    tmp_path = str(year_dir / f"tmp_{year}_{month:02d}.pbf")
                    final_path = str(year_dir / f"snapshot_{year}_{month:02d}.pbf")
                    
                    # Skip if already exists
                    if Path(final_path).exists():
                        self._log(f'Snapshot already exists: {final_path}')
                        continue
                    
                    self._log(f'Generating snapshot for {year}-{month:02d}...')
                    
                    # Run osmium time-filter
                    subprocess.run([
                        osmium_bin, 'time-filter', region_pbf.path, target_date,
                        '-o', tmp_path, '--overwrite'
                    ], check=True, capture_output=True)
                    
                    # Convert history format to flat snapshot format
                    subprocess.run([
                        osmium_bin, 'cat', tmp_path, '-o', final_path, '-f', 'pbf', '--overwrite'
                    ], check=True, capture_output=True)
                    
                    Path(tmp_path).unlink(missing_ok=True)
                    
                    # Create PbfFile record for this monthly snapshot
                    monthly_pbf = PbfFile.objects.create(
                        path=final_path,
                        pbf_file_type='REGION',
                        extraction_level='REGION_MONTHLY',
                        parent_pbf=region_pbf,
                        status='COMPLETED',
                        max_timestamp=datetime(year, month, last_day, 23, 59, 59),
                        has_history=False
                    )
                    
                    # Track monthly snapshot as PipelineAsset with lineage
                    self._create_asset(
                        asset_type='PBF',
                        asset_path=final_path,
                        parent_asset=region_asset,
                        stage_name='monthly_snapshots',
                        metadata={
                            'pbf_type': 'REGION_MONTHLY',
                            'year': year,
                            'month': month,
                            'target_date': target_date,
                            'parent_pbf': str(region_pbf.path)
                        }
                    )
                    
                    snapshots_created += 1
                    self._log(f'Created snapshot: {final_path}')
            
            self._step_done('monthly_snapshots', f'{snapshots_created} monthly snapshots generated successfully')
            
        except FileNotFoundError:
            self._log('osmium command not found. Skipping snapshot generation.')
            self._step_done('monthly_snapshots', 'Skipped (osmium not installed)')
        except subprocess.CalledProcessError as e:
            self._log(f'Osmium error: {e.stderr}')
            self._step_done('monthly_snapshots', 'Skipped (history-to-snapshot error)')
        except Exception as str_e:
            self._log(f'Error during snapshot conversion: {str_e}')
            self._step_done('monthly_snapshots', 'Skipped due to internal error')

    def _step5_embed_osm_entities(self, region_pbf):
        self._step_start('embed_osm_entities', 'Ingesting OSM entities + GV-Tags embeddings...')
        
        # Try to use the latest monthly snapshot if available
        latest_monthly = region_pbf.get_latest_monthly_extract()
        
        if latest_monthly:
            pbf_path = latest_monthly.path
            self._log(f'Using latest monthly snapshot: {pbf_path} (timestamp: {latest_monthly.max_timestamp})')
        else:
            pbf_path = region_pbf.path
            self._log(f'No monthly snapshots found, using region PBF: {pbf_path}')
        
        total_entities = 0
        try:
            osmium_bin = getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium')
            result = subprocess.run([
                osmium_bin, 'fileinfo', '-e', '-g', 'data.count.nodes,data.count.ways,data.count.relations', pbf_path
            ], capture_output=True, text=True, check=True)
            counts = result.stdout.strip().split('\n')
            for c in counts:
                if c.strip().isdigit():
                    total_entities += int(c.strip())
            self._log(f'PBF pre-scan complete: {total_entities:,} entities found.')
        except Exception as e:
            self._log(f'Could not run osmium fileinfo for pre-scan: {e}. Defaulting to 1000000.')
            total_entities = 1000000  # fallback non-zero
            
        call_command(
            'extract_osm_embeddings',
            pbf_file=pbf_path,
            chunk_size=20000,
            batch_size=5000,
            workers=4,
            websocket_group=self.group_name,
            total_entities=total_entities
        )
        
        # Track embedding ingestion as PipelineAsset (database operation)
        self._create_asset(
            asset_type='EMBEDDING',
            asset_path=f'db:vectors:OsmEntity:{self.iso_code}',
            parent_asset=None,
            stage_name='embed_osm_entities',
            metadata={
                'pbf_source': pbf_path,
                'total_entities': total_entities,
                'embedding_type': 'gv_tags'
            }
        )
        
        self._step_done('embed_osm_entities', 'OSM entities ingested into vectors DB')
        
        # Close DB connections to free memory before heavy queries
        from django.db import connections
        for conn in connections.all():
            conn.close()

    def _step5b_enrich_worldkg(self):
        self._step_start('enrich_worldkg',
                         f'Enriching entities with WorldKG classes for {self.country_name}...')
        from worldkg_nca.services.enrichment_service import get_worldkg_enrichment_service
        service = get_worldkg_enrichment_service()
        stats = service.batch_enrich_region(
            region=self.country_name,
            use_sparql=False,
            batch_size=1000,
            skip_enriched=True,
        )
        self._log(f'Enriched {stats["enriched"]} entities, {stats["failed"]} failed, '
                  f'{stats["local_predictions"]} via local matching')
        self._step_done('enrich_worldkg',
                        f'WorldKG enrichment complete — {stats["enriched"]} entities classified')

    def _step6_harvest_wikidata(self):
        cache_file = self._get_cache_path()

        if Path(cache_file).exists():
            import os as _os
            _os.remove(cache_file)
            self._log(f'Removed stale cache file: {cache_file}')

        kwargs = dict(enrich_classes=True)
        country_slug = self.country_name.lower().strip().replace(' ', '_').replace('-', '_')
        if self.iso_code:
            kwargs['country'] = self.iso_code
        else:
            poly_file = self._find_polygon_file(country_slug)
            if poly_file:
                kwargs['poly_file'] = poly_file
            else:
                self._log("Warning: No iso code or poly file available for country candidate scope.")
        kwargs['cache_file'] = cache_file

        call_command('harvest_wikidata_candidates', **kwargs)
        
        # Track Wikidata cache file as PipelineAsset
        if Path(cache_file).exists():
            self._create_asset(
                asset_type='PICKLE',
                asset_path=cache_file,
                parent_asset=None,
                stage_name='harvest_wikidata',
                metadata={
                    'cache_type': 'wikidata_candidates',
                    'enrich_classes': kwargs.get('enrich_classes', False)
                }
            )
        
        self._step_done('harvest_wikidata',
                        f'Wikidata harvest complete — cache: {cache_file}')

    def _step7_run_igea(self):
        self._step_start('run_igea',
                         f'Running IGEA entity alignment for {self.country_name}...')
        
        cache_file = self._get_cache_path()
        if not Path(cache_file).exists():
            self._log(f'Warning: IGEA cache file not found: {cache_file}. Skipping IGEA.')
            self._step_done('run_igea', 'Skipped (no candidate cache)')
            return
        
        from igea.services.iterative_alignment_service import IterativeEntityAlignmentService
        
        with open(cache_file, 'r') as f:
            import json
            candidates = json.load(f)
        
        self._log(f'Loaded {len(candidates)} Wikidata candidates from cache')
        
        igea = IterativeEntityAlignmentService(
            max_iterations=3,
            threshold=0.6,
        )
        igea.load_wikidata_candidates(candidates)
        stats = igea.run(country_code=self.iso_code)
        
        self._log(
            f'IGEA complete: {stats["total_accepted"]:,} accepted '
            f'over {stats["iterations_run"]} iterations'
        )
        
        # Track IGEA operation as PipelineAsset (database update)
        self._create_asset(
            asset_type='METRICS',
            asset_path=f'db:vectors:OsmEntity:igea:{self.iso_code}',
            parent_asset=None,
            stage_name='run_igea',
            metadata={
                'operation': 'entity_alignment',
                'candidate_cache': cache_file,
                'total_accepted': stats.get('total_accepted', 0),
                'iterations_run': stats.get('iterations_run', 0),
            }
        )
        
        self._step_done('run_igea',
                        f'IGEA complete — {stats["total_accepted"]:,} entities aligned')

    def _step8_predict_triples(self):
        self._step_start('predict_triples',
                         f'Scoring spatial triples for {self.country_name}...')
        
        # Check if any entities have spatial tags (prerequisite for triplet scoring)
        # TODO: double-check this logic - it might be too restrictive
        from worldkg_nca.models import OsmEntity
        spatial_keys = {
            'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
            'addr:country', 'addr:state', 'addr:county', 'addr:city',
            'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
        }
        # Count entities with geometry AND at least one spatial tag
        entities_with_spatial = OsmEntity.objects.using('vectors').filter(
            geom__isnull=False
        )
        spatial_count = 0
        for e in entities_with_spatial.iterator(chunk_size=10000):
            tags = e.tags or {}
            if any(k in tags for k in spatial_keys):
                spatial_count += 1
                if spatial_count >= 100:  # Enough to confirm data exists
                    break
        
        if spatial_count == 0:
            self._log('No entities with spatial literal tags found — skipping triplet scoring')
            self._step_done('predict_triples',
                           f'Skipped (0 entities with spatial tags)')
            return
        
        self._log(f'Found entities with spatial tags (sample: {spatial_count}+)')
        kwargs = {
            'limit': 50000,
            'threshold': 0.7,
            'gpu': True,  # Use GPU acceleration for USLP scoring
        }
        if self.iso_code:
            kwargs['country'] = self.country_name  # Use full name - OSM tags use "Belize" not "BZ"
        kwargs['session_id'] = self.session_id  # Pass session_id for websocket updates

        # Get snapshot_id from pipeline configuration or results
        snapshot_id = None
        if self.pipeline_run:
            config = self.pipeline_run.configuration or {}
            results = self.pipeline_run.results or {}
            snapshot_id = config.get('snapshot_id') or results.get('snapshot_id')

        if snapshot_id:
            kwargs['snapshot_id'] = str(snapshot_id)
            self._log(f'Using snapshot_id={snapshot_id} for spatial link prediction')

        call_command('predict_spatial_links', **kwargs)
        
        # Track spatial triples as PipelineAsset (database records)
        self._create_asset(
            asset_type='EMBEDDING',
            asset_path=f'db:vectors:WorldKGTriple:{self.iso_code}',
            parent_asset=None,
            stage_name='predict_triples',
            metadata={
                'operation': 'spatial_link_prediction',
                'limit': kwargs.get('limit', 50000),
            }
        )
        
        self._step_done('predict_triples',
                        'Triplet scoring complete — WorldKG triples created')

    def _step9_train_gv_nle(self):
        self._step_start('train_gv_nle',
                         f'Training GV-NLE spatial embeddings for {self.country_name}...')
        kwargs = dict(
            workers=4,
            batch_size=1000,
        )
        if self.iso_code:
            kwargs['country'] = self.iso_code
        else:
            country_slug = self.country_name.lower().strip().replace(' ', '_').replace('-', '_')
            poly_file = self._find_polygon_file(country_slug)
            if poly_file:
                kwargs['poly_file'] = poly_file
        
        call_command('train_gv_nle', **kwargs)
        
        # Track GV-NLE model as PipelineAsset (database update)
        self._create_asset(
            asset_type='EMBEDDING',
            asset_path=f'db:vectors:OsmEntity:gv_nle:{self.iso_code}',
            parent_asset=None,
            stage_name='train_gv_nle',
            metadata={
                'operation': 'gv_nle_training',
                'workers': kwargs.get('workers', 4),
                'batch_size': kwargs.get('batch_size', 1000)
            }
        )
        
        self._step_done('train_gv_nle',
                        'GV-NLE spatial model generated and database updated')

    def _step11_compute_static_embeddings(self):
        """
        Step 11: Compute 400D static_embedding = concat(GV-Tags 300D, GV-NLE 100D).

        Must run after train_gv_nle because GV-NLE embeddings are the prerequisite.
        The resulting static_embedding is used by the HNSW ANN index that powers
        the Semantic Triplet Search feature.
        """
        self._step_start('compute_static_embeddings',
                         f'Computing 400D static embeddings for {self.country_name}...')

        call_command('compute_static_embeddings')

        self._step_done('compute_static_embeddings',
                        'Static embeddings computed — HNSW index ready for ANN search')

    def _step10_validate_links(self):
        self._step_start('validate_links',
                         f'Validating low-confidence links for {self.country_name}...')

        from igea.services.google_places_validator import GooglePlacesValidator

        validator = GooglePlacesValidator()

        if not validator.enabled:
            # Compute cost estimate even without API key
            from igea.models import SpatialTripletScoreRejected
            from django.db.models import Q
            country_filter = Q(country_name__iexact=self.country_name)
            if self.iso_code and self.iso_code != self.country_name:
                country_filter |= Q(country_name__iexact=self.iso_code)
            rejected_count = SpatialTripletScoreRejected.objects.filter(country_filter).count()

            cost_per_call = 0.005
            estimated_cost = round(rejected_count * cost_per_call, 2)

            self._log(
                f'Google Places validation disabled (no API key configured). '
                f'Would validate {rejected_count:,} rejected links '
                f'at an estimated cost of ${estimated_cost:.2f} '
                f'(geocoding @ $0.005/call).'
            )

            # Store cost estimate in pipeline run results
            if self.pipeline_run:
                self.pipeline_run.results['validation_cost_estimate'] = {
                    'rejected_links': rejected_count,
                    'cost_per_call_usd': cost_per_call,
                    'estimated_total_cost_usd': estimated_cost,
                    'api_enabled': False,
                }
                self.pipeline_run.save(update_fields=['results'])

            self._step_done('validate_links',
                            f'Skipped (no API key) — {rejected_count:,} links would cost ~${estimated_cost:.2f}')
            return

        # API key is configured — run actual validation
        from igea.models import SpatialTripletScoreRejected, SpatialTripletScore
        from worldkg_nca.models import OsmEntity

        from django.db.models import Q
        country_filter = Q(country_name__iexact=self.country_name)
        if self.iso_code and self.iso_code != self.country_name:
            country_filter |= Q(country_name__iexact=self.iso_code)
        rejected_links = SpatialTripletScoreRejected.objects.filter(
            country_filter
        ).select_related()[:5000]  # Limit to 5000 for cost control

        total_rejected = rejected_links.count()
        if total_rejected == 0:
            self._step_done('validate_links', 'No rejected links to validate')
            return

        self._log(f'Validating {total_rejected:,} rejected links via Google Places...')

        validated_count = 0
        promoted_count = 0

        for link in rejected_links.iterator(chunk_size=100):
            try:
                head_entity = OsmEntity.objects.using('vectors').filter(
                    osm_id=link.head_osm_id
                ).first()
                tail_entity = OsmEntity.objects.using('vectors').filter(
                    osm_id=link.tail_osm_id
                ).first()

                if not head_entity or not tail_entity:
                    continue

                head_name = head_entity.name or ''
                tail_name = tail_entity.name or ''
                head_lat = head_entity.lat or 0.0
                head_lon = head_entity.lon or 0.0
                tail_lat = tail_entity.lat or 0.0
                tail_lon = tail_entity.lon or 0.0

                google_score = validator.validate_link(
                    head_name, head_lat, head_lon,
                    tail_name, tail_lat, tail_lon,
                    link.relation,
                )

                if google_score is None:
                    continue

                validated_count += 1

                # If Google confirms with high confidence, promote to accepted
                if google_score >= 0.5:
                    SpatialTripletScore.objects.create(
                        head_osm_type=link.head_osm_type,
                        head_osm_id=link.head_osm_id,
                        tail_osm_type=link.tail_osm_type,
                        tail_osm_id=link.tail_osm_id,
                        relation=link.relation,
                        geo_score=link.geo_score,
                        name_score=link.name_score,
                        topo_score=link.topo_score,
                        unnormalized_score=link.unnormalized_score,
                        normalized_score=link.normalized_score,
                        geohash_precision=link.geohash_precision,
                        snapshot_id=link.snapshot_id,
                        country_name=link.country_name,
                    )
                    promoted_count += 1

            except Exception as exc:
                logger.warning(f'Validation error for link {link}: {exc}')
                continue

        usage = validator.usage_tracker.summary
        self._log(
            f'Validation complete: {validated_count:,} validated, '
            f'{promoted_count:,} promoted to accepted. '
            f'API calls: {usage["call_count"]:,}, '
            f'Cost: ${usage["total_cost_usd"]:.2f}'
        )

        if self.pipeline_run:
            self.pipeline_run.results['validation_results'] = {
                'total_rejected': total_rejected,
                'validated_count': validated_count,
                'promoted_count': promoted_count,
                'api_calls': usage['call_count'],
                'total_cost_usd': usage['total_cost_usd'],
                'api_enabled': True,
            }
            self.pipeline_run.save(update_fields=['results'])

        self._step_done('validate_links',
                        f'{promoted_count:,} links promoted via Google Places validation')

    def _step12_learn_projection_weights(self):
        """Learn country-specific projection weights from SpatialTripletScore tables."""
        self._step_start(
            'learn_projection_weights',
            f'Learning projection weights for {self.country_name}...',
        )

        service = ProjectionWeightService()
        config = {
            'country_name': self.country_name,
            'iso_code': self.iso_code,
            'grid_size': 4,
            'min_weight': 0.5,
            'max_weight': 2.0,
            'max_samples': 50000,
        }

        result = service.execute(config)

        if self.pipeline_run and result.get('weights'):
            key = self.iso_code or self.country_name
            self.pipeline_run.results.setdefault('projection_weights', {})[key] = result
            self.pipeline_run.save(update_fields=['results'])

        message = (
            'Projection weights learned'
            if result.get('weights')
            else 'Skipped (insufficient validation data)'
        )
        self._step_done('learn_projection_weights', message)

    def _step13_precompute_link_candidates(self):
        self._step_start(
            'precompute_link_candidates',
            f'Precomputing link candidates for {self.country_name}...',
        )

        try:
            summary = run_precompute_for_region(
                country_name=self.country_name,
                iso_code=self.iso_code,
                top_k=10,
                batch_size=500,
            )
            processed = summary.get('processed', 0)
            message = f'Precomputed link candidates for {processed:,} entities'
            self._step_done('precompute_link_candidates', message)
        except Exception as exc:
            self._log(f'Precompute link candidates failed: {exc}')
            self._step_done(
                'precompute_link_candidates',
                'Skipped (error during precompute)',
            )

    def _last_days_of_months(self, start_year, end_year):
        """Return the last calendar day of every month between start_year and end_year."""
        from datetime import date, timedelta
        days = []
        year = start_year
        month = 1
        while year <= end_year:
            if month == 12:
                last = date(year, 12, 31)
            else:
                last = date(year, month + 1, 1) - timedelta(days=1)
            days.append(last)
            month += 1
            if month > 12:
                month = 1
                year += 1
        return days

    def _find_polygon_file(self, country_slug):
        poly_dir_setting = settings.POLYGON_FILES_DIR or 'data/osm_polygon_files'
        poly_dir = Path(poly_dir_setting)
        
        # If relative path, resolve it relative to BASE_DIR
        if not poly_dir.is_absolute():
            poly_dir = Path(settings.BASE_DIR) / poly_dir
        
        if not poly_dir.exists():
            self._log(f'Warning: Polygon directory does not exist: {poly_dir}')
            return None
            
        for ext in ['.poly', '.geojson']:
            matches = list(poly_dir.rglob(f'{country_slug}{ext}'))
            if matches:
                self._log(f'Found polygon file: {matches[0]}')
                return str(matches[0])
        
        self._log(f'No polygon file found for {country_slug} in {poly_dir}')
        return None

    def _find_continent_pbf(self, country_slug):
        """
        Find the correct continent PBF for a given country using RegionHierarchy.
        
        Args:
            country_slug: Country name (e.g., 'jamaica')
        
        Returns:
            PbfFile: Continent PBF or Planet PBF fallback
        """
        from api.models import PbfFile, RegionHierarchy
        
        # Find the country in RegionHierarchy
        country_region = RegionHierarchy.objects.filter(
            name__iexact=country_slug
        ).first()
        
        if country_region and country_region.parent:
            # Get the continent (parent of country)
            continent_region = country_region.parent
            
            # Check if continent has a linked PBF
            if continent_region.corresponding_pbf:
                self._log(f'Found continent PBF via hierarchy: {continent_region.name} → {continent_region.corresponding_pbf.path}')
                return continent_region.corresponding_pbf
            
            # Fallback: Try to find continent PBF by name
            continent_pbf = PbfFile.objects.filter(
                pbf_file_type='CONTINENT',
                path__icontains=continent_region.name,
                status='COMPLETED'
            ).first()
            
            if continent_pbf:
                self._log(f'Found continent PBF by name match: {continent_region.name} → {continent_pbf.path}')
                return continent_pbf
            
            self._log(f'No continent PBF found for {continent_region.name}, falling back to planet')
        else:
            self._log(f'Country {country_slug} not found in RegionHierarchy, falling back to planet')
        
        # Fallback to PLANET PBF if no continent found
        planet = PbfFile.objects.filter(
            pbf_file_type='PLANET',
            status='COMPLETED'
        ).order_by('-created_at').first()
        
        if planet:
            self._log(f'Using planet PBF (no continent found for {country_slug}): {planet.path}')
            return planet
        
        self._log('No CONTINENT or PLANET PBF file found')
        return None
