"""
Search Update Orchestrator Service

Orchestrates the full pipeline: Yearly → Monthly → Assets → GeoVectors → Search
With Google Drive backup and artifact lifecycle management.

Steps 4–6 are implemented using techniques from:
  - USLP (Mann et al., ISWC 2023): inductive GV-NLE embeddings for new entities
  - IGEA (Dsouza et al., ISWC 2023): iterative entity alignment for Wikidata links
"""

import os
import logging
import shutil
import uuid
from pathlib import Path
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from django.contrib.gis.geos import Point
from django.db import transaction

from extraction.services.temporal_extract_service import TemporalExtractService
from orchestration.services.drive_sync_verifier import DriveSyncVerifier
from api.models import (
    PbfFile,
    ProcessingSession,
    CountryArtifact,
    EmbeddingArtifact,
)

logger = logging.getLogger(__name__)


class SearchUpdateOrchestrator:
    """
    Orchestrates the full pipeline: Yearly → Monthly → Assets → GeoVectors → Search
    """
    
    def __init__(self):
        self.temporal_service = TemporalExtractService()
        
        # Initialize Google Drive sync verifier if path is configured
        drive_sync_path = getattr(settings, 'GOOGLE_DRIVE_SYNC_PATH', None)
        if drive_sync_path:
            self.drive_verifier = DriveSyncVerifier(drive_sync_path)
        else:
            self.drive_verifier = None
            logger.warning("GOOGLE_DRIVE_SYNC_PATH not configured")
    
    def execute_full_pipeline(self, config):
        """
        Execute the complete search update pipeline.
        
        Args:
            config: {
                'country_code': 'GRD',
                'country_name': 'Grenada',
                'start_year': 2020,
                'end_year': 2024,
                'google_drive_backup': True
            }
        
        Returns:
            dict: Pipeline execution results
        """
        # Create processing session
        session = ProcessingSession.objects.create(
            session_name=f"Search Update: {config['country_name']}",
            session_type='SEARCH_UPDATE',
            configuration=config,
            status='IN_PROGRESS'
        )
        
        results = {
            'session_id': str(session.id),
            'status': 'in_progress',
            'current_step': 0,
            'total_steps': 7,
            'steps_completed': [],
            'artifacts': {
                'yearly_extracts': [],
                'monthly_extracts': [],
                'asset_bundles': [],
                'fasttext_models': [],
                'embeddings': []
            }
        }
        
        try:
            # Step 1: Generate Yearly Extracts
            logger.info(f"Step 1/6: Generating yearly extracts for {config['country_name']}")
            results['current_step'] = 1
            results['status_message'] = f"Generating yearly extracts ({config['start_year']}-{config['end_year']})"
            self._update_session(session, results)
            
            yearly_result = self._generate_yearly_extracts(config, session)
            results['steps_completed'].append('yearly_extracts')
            results['artifacts']['yearly_extracts'] = yearly_result.get('artifact_ids', [])
            
            # Step 2: Backup to Google Drive
            if config.get('google_drive_backup', True) and self.drive_verifier:
                logger.info("Step 2/6: Backing up yearly extracts to Google Drive")
                results['current_step'] = 2
                results['status_message'] = "Uploading yearly extracts to Google Drive"
                self._update_session(session, results)
                
                drive_result = self._backup_to_drive(yearly_result, config)
                results['steps_completed'].append('google_drive_backup')
                results['google_drive_sync_path'] = drive_result['sync_path']
                results['backup_confirmed'] = drive_result['confirmed']
                
                # Step 2.5: Delete Yearly Extracts (after backup confirmed)
                if drive_result['confirmed']:
                    self._delete_yearly_extracts(yearly_result, config)
                    results['yearly_extracts_deleted'] = True
                    results['steps_completed'].append('yearly_deleted')
            else:
                results['current_step'] = 2
                results['steps_completed'].append('google_drive_backup_skipped')
            
            # Step 3: Generate Monthly Extracts (KEEP THESE)
            logger.info("Step 3/6: Generating monthly extracts")
            results['current_step'] = 3
            results['status_message'] = "Generating monthly extracts from yearly data"
            self._update_session(session, results)
            
            monthly_result = self._generate_monthly_extracts(yearly_result, config, session)
            results['steps_completed'].append('monthly_extracts')
            results['artifacts']['monthly_extracts'] = monthly_result.get('artifact_ids', [])
            
            # Step 4: Parse Asset Bundle (entity list from monthly PBF extracts)
            logger.info("Step 4/7: Parsing asset bundles from monthly PBF extracts")
            results['current_step'] = 4
            results['status_message'] = "Extracting entity list from monthly PBF files"
            self._update_session(session, results)

            asset_bundle = self._parse_asset_bundle(monthly_result, config)
            results['steps_completed'].append('asset_bundles')
            results['artifacts']['asset_bundles'] = asset_bundle.get('summary', {})

            # Step 5: Generate GV-Tags + inductive GV-NLE embeddings
            logger.info("Step 5/7: Generating GeoVectors embeddings")
            results['current_step'] = 5
            results['status_message'] = "Computing GV-Tags (FastText) and GV-NLE (inductive) embeddings"
            self._update_session(session, results)

            embedding_result = self._generate_embeddings(asset_bundle, config, session)
            results['steps_completed'].append('embeddings')
            results['artifacts']['embeddings'] = embedding_result.get('summary', {})

            # Step 6: Upsert to pgvector DB + compute snapshot diff
            logger.info("Step 6/7: Upserting entities and computing snapshot diff")
            results['current_step'] = 6
            results['status_message'] = "Upserting OsmEntity records and tracking snapshot diff"
            self._update_session(session, results)

            diff_result = self._upsert_and_diff(embedding_result, config, session)
            results['steps_completed'].append('search_index')
            results['snapshot_diff'] = diff_result

            # Step 7: Predict spatial links (USLP) + align Wikidata entities (IGEA)
            logger.info("Step 7/7: USLP spatial link prediction + IGEA entity alignment")
            results['current_step'] = 7
            results['status_message'] = "Running spatial link prediction and entity alignment"
            self._update_session(session, results)

            enrichment_result = self._run_enrichment(config, diff_result, session)
            results['steps_completed'].append('enrichment')
            results['enrichment'] = enrichment_result

            # Mark session as completed
            results['status'] = 'completed'
            results['status_message'] = f"Search embeddings updated for {config['country_name']}"
            session.status = 'COMPLETED'
            session.completed_at = timezone.now()
            session.results = results
            session.save()

            logger.info(f"Pipeline completed successfully for {config['country_name']}")
            return results
            
        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            results['status'] = 'failed'
            results['error'] = str(e)
            session.status = 'FAILED'
            session.results = results
            session.save()
            raise
    
    def _generate_yearly_extracts(self, config, session):
        """Generate yearly extracts using TemporalExtractService."""
        # Find region PBF
        region_pbf = PbfFile.objects.filter(
            path__icontains=config['country_name'].lower(),
            extraction_level='REGION'
        ).first()
        
        if not region_pbf:
            raise ValueError(f"No region extract found for {config['country_name']}")
        
        # Generate yearly extracts
        output_root = getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', '/data/temporal-extracts')
        result = self.temporal_service.generate_temporal_extracts({
            'source_pbf_id': str(region_pbf.id),
            'granularity': 'yearly',
            'start_year': config['start_year'],
            'end_year': config['end_year'],
            'output_directory': output_root
        })
        
        # Track artifacts in database
        yearly_artifacts = []
        for extract_id in result.get('extract_ids', []):
            pbf = PbfFile.objects.get(id=extract_id)
            
            artifact = CountryArtifact.objects.create(
                country_code=config.get('country_code', ''),
                country_name=config['country_name'],
                artifact_type='YEARLY_EXTRACT',
                artifact_path=pbf.path,
                artifact_status='ACTIVE',
                temporal_start=pbf.min_timestamp.date() if pbf.min_timestamp else None,
                temporal_end=pbf.max_timestamp.date() if pbf.max_timestamp else None,
                file_size_mb=os.path.getsize(pbf.path) / (1024 * 1024) if os.path.exists(pbf.path) else None,
                processing_session=session
            )
            yearly_artifacts.append(artifact)
        
        result['extract_paths'] = [a.artifact_path for a in yearly_artifacts]
        result['artifact_ids'] = [str(a.id) for a in yearly_artifacts]
        
        return result
    
    def _backup_to_drive(self, yearly_result, config):
        """
        Copy yearly extracts to local Google Drive sync folder.
        No API calls - just file copy to mounted drive.
        """
        if not self.drive_verifier:
            return {
                'sync_path': None,
                'copied_files': [],
                'confirmed': False,
                'error': 'Google Drive sync path not configured'
            }
        
        # Local Google Drive sync folder (mounted on your system)
        drive_sync_base = self.drive_verifier.sync_path
        
        # Create country-specific folder
        country_folder = drive_sync_base / 'OSM_Yearly_Extracts' / config['country_name']
        country_folder.mkdir(parents=True, exist_ok=True)
        
        copied_files = []
        
        for extract_path in yearly_result.get('extract_paths', []):
            source = Path(extract_path)
            destination = country_folder / source.name
            
            # Copy file to Google Drive sync folder
            shutil.copy2(source, destination)
            copied_files.append(str(destination))
            logger.info(f"Copied to Google Drive sync: {destination}")
            
            # Update artifact with Google Drive path
            artifact = CountryArtifact.objects.filter(artifact_path=extract_path).first()
            if artifact:
                artifact.google_drive_path = str(destination)
                artifact.save()
        
        # Verify files exist in sync folder
        confirmed = all(Path(f).exists() for f in copied_files)
        
        # Update artifacts with backup confirmation
        if confirmed:
            CountryArtifact.objects.filter(
                artifact_path__in=yearly_result.get('extract_paths', [])
            ).update(backup_confirmed=True)
        
        return {
            'sync_path': str(country_folder),
            'copied_files': copied_files,
            'confirmed': confirmed
        }
    
    def _delete_yearly_extracts(self, yearly_result, config):
        """
        Delete yearly extracts after Google Drive backup confirmed.
        Keep monthly extracts for sparse graph use cases.
        """
        for extract_path in yearly_result.get('extract_paths', []):
            try:
                # Delete file from disk
                if os.path.exists(extract_path):
                    os.remove(extract_path)
                    logger.info(f"Deleted yearly extract: {extract_path}")
                
                # Update database record
                pbf = PbfFile.objects.filter(path=extract_path).first()
                if pbf:
                    pbf.status = 'DELETED'
                    pbf.save()
                
                # Update artifact record
                artifact = CountryArtifact.objects.filter(artifact_path=extract_path).first()
                if artifact:
                    artifact.artifact_status = 'DELETED'
                    artifact.deleted_at = timezone.now()
                    artifact.deletion_reason = 'Backed up to Google Drive, no longer needed locally'
                    artifact.save()
                    
            except Exception as e:
                logger.error(f"Failed to delete {extract_path}: {e}")
    
    def _generate_monthly_extracts(self, yearly_result, config, session):
        """Generate monthly extracts from yearly extracts."""
        yearly_pbf_ids = yearly_result.get('extract_ids', [])
        
        all_monthly_artifacts = []
        
        for yearly_pbf_id in yearly_pbf_ids:
            result = self.temporal_service.generate_temporal_extracts({
                'source_pbf_id': yearly_pbf_id,
                'granularity': 'monthly',
                'start_year': config['start_year'],
                'end_year': config['end_year']
            })
            
            # Track monthly artifacts
            for extract_id in result.get('extract_ids', []):
                pbf = PbfFile.objects.get(id=extract_id)
                
                artifact = CountryArtifact.objects.create(
                    country_code=config.get('country_code', ''),
                    country_name=config['country_name'],
                    artifact_type='MONTHLY_EXTRACT',
                    artifact_path=pbf.path,
                    artifact_status='ACTIVE',
                    temporal_start=pbf.min_timestamp.date() if pbf.min_timestamp else None,
                    temporal_end=pbf.max_timestamp.date() if pbf.max_timestamp else None,
                    file_size_mb=os.path.getsize(pbf.path) / (1024 * 1024) if os.path.exists(pbf.path) else None,
                    processing_session=session
                )
                all_monthly_artifacts.append(artifact)
        
        return {
            'success': True,
            'extracts_created': len(all_monthly_artifacts),
            'extract_paths': [a.artifact_path for a in all_monthly_artifacts],
            'artifact_ids': [str(a.id) for a in all_monthly_artifacts]
        }
    
    # ------------------------------------------------------------------
    # Step 4: Parse asset bundle from monthly PBF extracts
    # ------------------------------------------------------------------

    def _parse_asset_bundle(self, monthly_result, config):
        """
        Parse monthly PBF extracts to produce a structured entity list.

        Uses osmium to iterate tagged nodes from each monthly PBF file.
        Returns a bundle dict used by _generate_embeddings.
        """
        import osmium

        class _TaggedNodeHandler(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.entities = []

            def node(self, n):
                if len(n.tags) > 0 and n.location.valid():
                    self.entities.append({
                        'osm_type': 'node',
                        'osm_id': n.id,
                        'tags': dict(n.tags),
                        'lat': n.location.lat,
                        'lon': n.location.lon,
                        'version': n.version,
                        'timestamp': n.timestamp,
                    })

            def way(self, w):
                if len(w.tags) > 0:
                    self.entities.append({
                        'osm_type': 'way',
                        'osm_id': w.id,
                        'tags': dict(w.tags),
                        'lat': None,
                        'lon': None,
                        'version': w.version,
                        'timestamp': w.timestamp,
                    })

            def relation(self, r):
                if len(r.tags) > 0:
                    self.entities.append({
                        'osm_type': 'relation',
                        'osm_id': r.id,
                        'tags': dict(r.tags),
                        'lat': None,
                        'lon': None,
                        'version': r.version,
                        'timestamp': r.timestamp,
                    })

        all_entities = []
        pbf_paths = monthly_result.get('extract_paths', [])

        for pbf_path in pbf_paths:
            if not os.path.exists(pbf_path):
                logger.warning(f"PBF not found, skipping: {pbf_path}")
                continue
            handler = _TaggedNodeHandler()
            handler.apply_file(pbf_path)
            all_entities.extend(handler.entities)
            logger.info(f"Parsed {len(handler.entities)} entities from {pbf_path}")

        # Deduplicate by (osm_type, osm_id) — keep latest version
        seen = {}
        for e in all_entities:
            key = (e['osm_type'], e['osm_id'])
            if key not in seen or (e.get('version') or 0) > (seen[key].get('version') or 0):
                seen[key] = e

        deduped = list(seen.values())
        logger.info(f"Asset bundle: {len(deduped)} unique entities from {len(pbf_paths)} PBFs")

        return {
            'entities': deduped,
            'summary': {
                'total_entities': len(deduped),
                'pbf_files_parsed': len(pbf_paths),
                'nodes': sum(1 for e in deduped if e['osm_type'] == 'node'),
                'ways': sum(1 for e in deduped if e['osm_type'] == 'way'),
                'relations': sum(1 for e in deduped if e['osm_type'] == 'relation'),
            },
        }

    # ------------------------------------------------------------------
    # Step 5: Generate GV-Tags (FastText) + GV-NLE (inductive) embeddings
    # ------------------------------------------------------------------

    def _generate_embeddings(self, asset_bundle, config, session):
        """
        Compute dual GeoVectors embeddings for all entities in the asset bundle.

        GV-Tags: computed immediately via FastText (entity-local, no retraining).
        GV-NLE:  computed via InductiveSpatialService for nodes with coordinates
                 (proximity-weighted mean of k=50 nearest trained entities).
                 Full DeepWalk retraining should be run monthly via train_gv_nle.

        Strategy from USLP (Mann et al. ISWC 2023): inductive inference over
        the existing embedding pool eliminates per-snapshot full retraining.
        """
        from semantic_search.services.fasttext_service import FastTextEmbeddingService
        from semantic_search.services.inductive_spatial_service import InductiveSpatialService

        entities = asset_bundle.get('entities', [])
        if not entities:
            return {'entities': [], 'summary': {'total': 0}}

        # Load inductive spatial service pool from DB
        inductive_svc = InductiveSpatialService(k=50)
        pool_size = inductive_svc.load_pool_from_db()
        logger.info(f"Inductive GV-NLE pool: {pool_size} trained entities")

        processed = []
        gv_tags_count = 0
        gv_nle_inductive_count = 0

        for entity in entities:
            tags = entity.get('tags', {})
            tag_counts = {f"{k}={v}": 1 for k, v in tags.items()}

            # GV-Tags: FastText weighted average over tag key=value tokens
            gv_tags_emb = FastTextEmbeddingService.calculate_embedding(tag_counts)
            gv_tags_count += 1

            # GV-NLE: inductive proximity-weighted embedding for nodes
            gv_nle_emb = None
            if entity.get('lat') is not None and pool_size > 0:
                gv_nle_emb = inductive_svc.embed_entity(entity['lat'], entity['lon'])
                if gv_nle_emb is not None:
                    gv_nle_inductive_count += 1

            processed.append({
                **entity,
                'gv_tags_embedding': gv_tags_emb.tolist() if gv_tags_emb is not None else None,
                'gv_nle_embedding': gv_nle_emb.tolist() if gv_nle_emb is not None else None,
            })

        logger.info(
            f"Embeddings: {gv_tags_count} GV-Tags, "
            f"{gv_nle_inductive_count} GV-NLE (inductive)"
        )

        return {
            'entities': processed,
            'summary': {
                'total': len(processed),
                'gv_tags_computed': gv_tags_count,
                'gv_nle_inductive': gv_nle_inductive_count,
                'pool_size': pool_size,
            },
        }

    # ------------------------------------------------------------------
    # Step 6: Upsert to pgvector DB + compute SnapshotDiff
    # ------------------------------------------------------------------

    def _upsert_and_diff(self, embedding_result, config, session):
        """
        Upsert OsmEntity rows for all embedded entities and record a SnapshotDiff.

        For each entity:
        - INSERT new row if (osm_type, osm_id) is unseen  → entities_added
        - UPDATE existing row if tags changed               → entities_modified
        - Entities absent from current but present before  → entities_deleted (soft-marked)

        Snapshot provenance is tracked via source_snapshot_id on each OsmEntity.
        """
        from worldkg_nca.models import OsmEntity
        from semantic_search.models import SnapshotDiff
        from django.contrib.gis.geos import Point

        entities = embedding_result.get('entities', [])
        snapshot_id = uuid.uuid4()   # new snapshot UUID for this run

        # Collect previous snapshot's entity IDs (for soft-delete tracking)
        prev_ids = set(
            OsmEntity.objects.using('vectors')
            .values_list('osm_id', flat=True)
        )

        added = modified = inductive_count = 0
        BATCH = 500
        batch = []

        current_ids = set()
        for entity in entities:
            osm_type = entity['osm_type']
            osm_id = entity['osm_id']
            tags = entity.get('tags', {})
            gv_tags_emb = entity.get('gv_tags_embedding')
            gv_nle_emb = entity.get('gv_nle_embedding')

            geom = None
            if entity.get('lat') is not None and entity.get('lon') is not None:
                geom = Point(entity['lon'], entity['lat'], srid=4326)

            current_ids.add(osm_id)

            try:
                existing = OsmEntity.objects.using('vectors').get(
                    osm_type=osm_type, osm_id=osm_id
                )
                # Update if tags or embeddings changed
                existing.tags = tags
                existing.geom = geom
                existing.version = entity.get('version')
                existing.timestamp = entity.get('timestamp')
                existing.source_snapshot_id = snapshot_id
                if gv_tags_emb is not None:
                    existing.gv_tags_embedding = gv_tags_emb
                if gv_nle_emb is not None:
                    existing.gv_nle_embedding = gv_nle_emb
                    existing.gv_nle_trained = True
                batch.append(('update', existing))
                modified += 1
            except OsmEntity.DoesNotExist:
                new_entity = OsmEntity.create_from_osm(
                    osm_type=osm_type,
                    osm_id=osm_id,
                    tags=tags,
                    gv_tags_embedding=gv_tags_emb,
                    gv_nle_embedding=gv_nle_emb,
                    geom=geom,
                    version=entity.get('version'),
                    timestamp=entity.get('timestamp'),
                )
                new_entity.source_snapshot_id = snapshot_id
                batch.append(('insert', new_entity))
                added += 1

            if entity.get('gv_nle_embedding') is not None:
                inductive_count += 1

            # Flush batch
            if len(batch) >= BATCH:
                self._flush_entity_batch(batch)
                batch = []

        if batch:
            self._flush_entity_batch(batch)

        # Soft-mark deleted entities (present before, absent now)
        deleted_ids = prev_ids - current_ids
        if deleted_ids:
            OsmEntity.objects.using('vectors').filter(
                osm_id__in=deleted_ids
            ).update(
                gv_nle_trained=False,
                gv_nle_version=None,
            )
        deleted = len(deleted_ids)

        # Persist SnapshotDiff
        diff = SnapshotDiff.objects.create(
            snapshot_id=snapshot_id,
            country_code=config.get('country_code', ''),
            country_name=config.get('country_name', ''),
            entities_added=added,
            entities_modified=modified,
            entities_deleted=deleted,
            entities_re_embedded=modified,
            gv_nle_inductive_count=inductive_count,
        )

        logger.info(
            f"SnapshotDiff: +{added} added, ~{modified} modified, "
            f"-{deleted} deleted, {inductive_count} inductive GV-NLE"
        )

        return {
            'snapshot_id': str(snapshot_id),
            'diff_id': str(diff.id),
            'entities_added': added,
            'entities_modified': modified,
            'entities_deleted': deleted,
            'gv_nle_inductive_count': inductive_count,
        }

    def _flush_entity_batch(self, batch):
        """Bulk-write a mixed insert/update batch to pgvector DB."""
        inserts = [e for op, e in batch if op == 'insert']
        updates = [e for op, e in batch if op == 'update']

        if inserts:
            with transaction.atomic(using='vectors'):
                OsmEntity = inserts[0].__class__
                OsmEntity.objects.using('vectors').bulk_create(
                    inserts, batch_size=500, ignore_conflicts=True
                )

        if updates:
            fields = [
                'tags', 'geom', 'version', 'timestamp',
                'gv_tags_embedding', 'gv_nle_embedding',
                'gv_nle_trained', 'source_snapshot_id',
            ]
            with transaction.atomic(using='vectors'):
                updates[0].__class__.objects.using('vectors').bulk_update(
                    updates, fields, batch_size=500
                )

    # ------------------------------------------------------------------
    # Step 7: USLP spatial link prediction + IGEA entity alignment
    # ------------------------------------------------------------------

    def _run_enrichment(self, config, diff_result, session):
        """
        Run USLP spatial link prediction and simplified IGEA entity alignment
        for new/modified entities in the current snapshot.

        USLP: fills missing object-property triples (isInCountry, addrCity, …).
        IGEA: extends wikidata_uri alignment via iterative cosine-based matching.
        """
        from semantic_search.services.spatial_link_prediction_service import (
            SpatialLinkPredictionService,
        )
        from semantic_search.services.iterative_entity_alignment_service import (
            IterativeEntityAlignmentService,
        )
        from semantic_search.models import SnapshotDiff

        snapshot_id = diff_result.get('snapshot_id')
        enrichment_stats = {}

        # --- USLP spatial link prediction ---
        try:
            slp_service = SpatialLinkPredictionService()
            pool_size = slp_service.load_candidate_pool_from_db()
            logger.info(f"USLP candidate pool: {pool_size}")

            if pool_size > 0:
                # Only predict for new entities from this snapshot
                from worldkg_nca.models import OsmEntity
                qs = OsmEntity.objects.using('vectors').filter(
                    source_snapshot_id=snapshot_id,
                    geom__isnull=False,
                )
                head_entities = [
                    {
                        'osm_id': e.osm_id,
                        'lat': e.geom.y,
                        'lon': e.geom.x,
                        'tags': e.tags,
                    }
                    for e in qs.iterator(chunk_size=5_000)
                ]
                links = slp_service.predict_links_batch(head_entities)
                n_links = slp_service.persist_links(links, snapshot_id=snapshot_id)

                enrichment_stats['uslp_links_predicted'] = n_links
                logger.info(f"USLP: predicted {n_links} spatial links")

                # Update diff record
                if snapshot_id:
                    SnapshotDiff.objects.filter(snapshot_id=snapshot_id).update(
                        spatial_links_predicted=n_links
                    )
        except Exception as e:
            logger.error(f"USLP enrichment failed: {e}", exc_info=True)
            enrichment_stats['uslp_error'] = str(e)

        # --- IGEA iterative entity alignment ---
        try:
            igea_service = IterativeEntityAlignmentService(
                max_iterations=3,
                threshold=0.6,
                max_distance_m=2500,
            )
            igea_service.load_wikidata_candidates_from_db()
            igea_stats = igea_service.run(
                country_code=config.get('country_code'),
                snapshot_id=snapshot_id,
            )
            enrichment_stats['igea'] = igea_stats

            if snapshot_id:
                SnapshotDiff.objects.filter(snapshot_id=snapshot_id).update(
                    wikidata_links_added=igea_stats.get('total_accepted', 0)
                )
        except Exception as e:
            logger.error(f"IGEA enrichment failed: {e}", exc_info=True)
            enrichment_stats['igea_error'] = str(e)

        return enrichment_stats

    def _update_session(self, session, results):
        """Update processing session with current progress."""
        session.results = results
        session.save()
    
    def get_session_status(self, session_id):
        """Get current status of a processing session."""
        session = ProcessingSession.objects.get(id=session_id)
        
        return {
            'session_id': str(session.id),
            'status': session.status,
            'current_step': session.results.get('current_step', 0),
            'total_steps': session.results.get('total_steps', 6),
            'status_message': session.results.get('status_message', ''),
            'steps_completed': session.results.get('steps_completed', []),
            'google_drive_sync_path': session.results.get('google_drive_sync_path'),
            'error': session.results.get('error')
        }
