import logging
import json
import time
import random
from django.db import connections
from django.contrib.gis.geos import Point
from worldkg_nca.models import OsmEntity

logger = logging.getLogger(__name__)

class VectorStorageService:
    """
    Handles batching and upserting vectors into the OsmEntity model.
    """
    def __init__(self, batch_size=20000, model_type='tags', version=None,
                 snapshot_id=None, country_code=None):
        self.batch_size = batch_size
        self.model_type = model_type # 'tags' or 'nle'
        self.version = version or '1.0'
        self.buffer = []
        # Phase 6 partition keys — propagated into every upserted row.
        # Nullable on the monolith; required after cutover.
        self.snapshot_id = snapshot_id
        self.country_code = country_code

    def add(self, raw_data, vector):
        """
        raw_data is a list: [id, type, tags, (lat, lon optional)]
        """
        # Handle variable length records (Nodes have 5, Ways/Relations have 3 in first pass)
        osm_id = int(raw_data[0])
        osm_type_code = raw_data[1]
        tags = raw_data[2]
        lat = raw_data[3] if len(raw_data) > 3 else None
        lon = raw_data[4] if len(raw_data) > 4 else None
        
        # Map code to name (handle both short 'n' and full 'node' formats)
        type_map = {
            'n': 'node', 'node': 'node',
            'w': 'way',  'way': 'way',
            'r': 'relation', 'relation': 'relation'
        }
        osm_type = type_map.get(str(osm_type_code).lower(), 'node')
        
        # Prepare entity data
        entity_data = {
            'osm_type': osm_type,
            'osm_id': osm_id,
            'tags': tags,
            'geom': Point(lon, lat) if lat and lon else None,
        }
        
        if self.model_type == 'tags':
            entity_data['gv_tags_embedding'] = vector
        else:
            entity_data['gv_nle_embedding'] = vector
            entity_data['gv_nle_trained'] = True

        self.buffer.append(entity_data)
        
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.buffer:
            return

        logger.info(f"Upserting {len(self.buffer)} entities into OsmEntity table...")
        
        # Use a manual upsert query or Django's update_or_create (slow)
        # For performance, we'll use a raw SQL bulk upsert
        self._bulk_upsert()
        
        self.buffer = []

    def _bulk_upsert(self):
        """Execute a raw SQL bulk upsert for high performance."""
        # Add small jitter to stagger workers and avoid deadlocks
        # Reduce range to minimize artificial delay on large single-country runs
        time.sleep(random.uniform(0, 0.1))
        
        import io
        import csv
        
        start_time = time.time()
        with connections['vectors'].cursor() as cursor:
            # Deduplicate buffer by (osm_type, osm_id) to prevent ON CONFLICT errors
            unique_items = {}
            for item in self.buffer:
                unique_items[(item['osm_type'], item['osm_id'])] = item

            # Build CSV buffer
            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer, delimiter='\t', quoting=csv.QUOTE_MINIMAL)
            col_name = "gv_tags_embedding" if self.model_type == 'tags' else "gv_nle_embedding"
            
            for item in unique_items.values():
                tags_dict = {str(k): str(v) for k, v in item['tags']}
                tags_json = json.dumps(tags_dict)
                geom_wkt = f"SRID=4326;POINT({item['geom'].x} {item['geom'].y})" if item['geom'] else "\\N"
                
                embedding = item.get('gv_tags_embedding')
                if embedding is None:
                    embedding = item.get('gv_nle_embedding')
                    
                if embedding is not None:
                    if hasattr(embedding, 'tolist'):
                        embedding = embedding.tolist()
                    vec_str = "[" + ",".join(map(str, embedding)) + "]"
                else:
                    vec_str = "\\N"
                    
                gv_nle_trained = 't' if item.get('gv_nle_trained') else 'f'

                # Phase 6 partition keys (NULL on monolith until backfilled)
                snap_id = self.snapshot_id if self.snapshot_id else "\\N"
                cc = self.country_code if self.country_code else "\\N"

                writer.writerow([
                    item['osm_type'],
                    item['osm_id'],
                    tags_json,
                    geom_wkt,
                    vec_str,
                    self.version,
                    gv_nle_trained,
                    snap_id,
                    cc
                ])
                
            csv_buffer.seek(0)
            
            # Temporary table creation
            temp_table = f"temp_upsert_{int(time.time() * 1000)}_{random.randint(0, 10000)}"
            try:
                cursor.execute(f"""
                    CREATE UNLOGGED TABLE {temp_table} (
                        osm_type VARCHAR(10),
                        osm_id BIGINT,
                        tags JSONB,
                        geom GEOMETRY(Point, 4326),
                        embedding VECTOR,
                        gv_tags_version VARCHAR(50),
                        gv_nle_trained BOOLEAN,
                        snapshot_id VARCHAR(20),
                        country_code VARCHAR(3)
                    );
                """)

                # Use psycopg2 cursor for copy_expert
                cursor.execute(f"CREATE INDEX ON {temp_table} (osm_type, osm_id);")
                psycopg_cursor = cursor.cursor if hasattr(cursor, 'cursor') else cursor.connection.cursor()
                psycopg_cursor.copy_expert(f"""
                    COPY {temp_table} (osm_type, osm_id, tags, geom, embedding, gv_tags_version, gv_nle_trained, snapshot_id, country_code)
                    FROM STDIN WITH (FORMAT csv, DELIMITER '\t', NULL '\\N')
                """, csv_buffer)

                # Phase 6: include partition keys in INSERT.
                # The ON CONFLICT target depends on whether the cutover has
                # happened.  On the monolith (WORLDKG_USE_PARTITIONED_TABLE=False)
                # the old 3-column constraint is used.  After cutover the
                # conflict target is widened to include snapshot_id, country_code
                # — see PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md Step 4.
                from django.conf import settings
                use_partitioned = getattr(
                    settings, 'WORLDKG_USE_PARTITIONED_TABLE', False
                )
                conflict_target = (
                    "(osm_type, osm_id, gv_tags_version, snapshot_id, country_code)"
                    if use_partitioned
                    else "(osm_type, osm_id, gv_tags_version)"
                )
                cursor.execute(f"""
                    INSERT INTO semantic_search_osmentity (
                        osm_type, osm_id, tags, geom, {col_name},
                        gv_tags_version, gv_nle_trained,
                        snapshot_id, country_code,
                        created_at, updated_at
                    )
                    SELECT
                        osm_type, osm_id, tags, geom, embedding,
                        gv_tags_version, gv_nle_trained,
                        snapshot_id, country_code,
                        NOW(), NOW()
                    FROM {temp_table}
                    ON CONFLICT {conflict_target} DO UPDATE SET
                        tags = EXCLUDED.tags,
                        geom = COALESCE(EXCLUDED.geom, semantic_search_osmentity.geom),
                        {col_name} = EXCLUDED.{col_name},
                        gv_nle_trained = EXCLUDED.gv_nle_trained,
                        snapshot_id = COALESCE(EXCLUDED.snapshot_id, semantic_search_osmentity.snapshot_id),
                        country_code = COALESCE(EXCLUDED.country_code, semantic_search_osmentity.country_code),
                        updated_at = NOW();
                """)
                
                duration = time.time() - start_time
                logger.info(f"  [SQL COPY] Upserted {len(unique_items)} entities in {duration:.2f}s")
            except Exception as e:
                logger.error(f"  [SQL ERROR] Failed to execute bulk COPY upsert: {e}", exc_info=True)
                raise e
            finally:
                cursor.execute(f"DROP TABLE IF EXISTS {temp_table};")
