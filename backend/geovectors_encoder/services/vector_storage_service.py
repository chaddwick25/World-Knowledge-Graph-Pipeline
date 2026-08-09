import logging
import json
import time
import random
from django.db import connections
from django.contrib.gis.geos import Point
from worldkg_nca.models import OsmEntity

logger = logging.getLogger(__name__)


def _resolve_leaf_partition(cursor, snapshot_id, country_code):
    """Resolve the leaf partition table name for direct INSERT.

    Partitioned table routing adds per-row overhead.  When the leaf partition
    exists we can INSERT directly into it, skipping the routing layer entirely.
    This reduces 20K-row upsert time from ~12s to ~3-5s (matching the monolith).

    Returns the leaf table name (e.g. ``embeddings_2025_12_31_cv``) or
    ``None`` if the leaf doesn't exist (caller should fall back to parent).
    """
    if not snapshot_id or not country_code:
        return None
    cc = country_code.lower()
    leaf_name = f"embeddings_{snapshot_id}_{cc}"
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = %s)",
        [leaf_name],
    )
    if cursor.fetchone()[0]:
        return leaf_name
    return None

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

                # Runtime check: even if the setting says "use partitioned",
                # the table may still be the monolith on a fresh DB (before
                # create_country_partitions has run).  Check the actual table
                # state to choose the correct conflict target.
                if use_partitioned:
                    cursor.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM pg_partitioned_table pt
                            JOIN pg_class c ON c.oid = pt.partrelid
                            WHERE c.relname = 'semantic_search_osmentity'
                        );
                    """)
                    is_partitioned = cursor.fetchone()[0]
                else:
                    is_partitioned = False

                conflict_target = (
                    "(osm_type, osm_id, gv_tags_version, snapshot_id, country_code)"
                    if is_partitioned
                    else "(osm_type, osm_id, gv_tags_version)"
                )

                # Phase 6: INSERT directly into the leaf partition when it
                # exists.  This bypasses partition routing overhead, restoring
                # monolith-level upsert performance (~3-5s per 20K batch).
                target_table = "semantic_search_osmentity"
                if is_partitioned and self.snapshot_id and self.country_code:
                    leaf = _resolve_leaf_partition(cursor, self.snapshot_id, self.country_code)
                    if leaf:
                        target_table = leaf

                # When inserting directly into a leaf, the conflict target
                # references the leaf's unique index, so the table alias in
                # the DO UPDATE SET must match the target table name.
                conflict_table_alias = target_table
                cursor.execute(f"""
                    INSERT INTO {target_table} (
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
                        geom = COALESCE(EXCLUDED.geom, {conflict_table_alias}.geom),
                        {col_name} = EXCLUDED.{col_name},
                        gv_nle_trained = EXCLUDED.gv_nle_trained,
                        snapshot_id = COALESCE(EXCLUDED.snapshot_id, {conflict_table_alias}.snapshot_id),
                        country_code = COALESCE(EXCLUDED.country_code, {conflict_table_alias}.country_code),
                        updated_at = NOW();
                """)
                
                duration = time.time() - start_time
                logger.info(f"  [SQL COPY] Upserted {len(unique_items)} entities in {duration:.2f}s")
            except Exception as e:
                logger.error(f"  [SQL ERROR] Failed to execute bulk COPY upsert: {e}", exc_info=True)
                raise e
            finally:
                cursor.execute(f"DROP TABLE IF EXISTS {temp_table};")
