"""SQL-side region enrichment (WorldKG class matching via UPDATE...FROM).

Extracted from ``worldkg_enrichment_service.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).  The SQL region path
(``sql_enrich_region`` + temp-table loading + leaf resolution) is a mixin
on ``WorldKGEnrichmentService``; the per-entity Python path
(``batch_enrich_region``) stays in the main service.
"""

import logging
from typing import Dict, Optional, Tuple

from django.utils import timezone

from core.services.planet_init.osm_wikidata_resolver import (
    resolve_country_bbox,
)

logger = logging.getLogger(__name__)


class WorldKGEnrichmentSqlMixin:
    """Provides the SQL join-based region enrichment path."""

    def _load_ontology_to_temp_table(self, cursor) -> None:
        """Load the in-memory ontology index into a temp table for SQL joins.

        Creates a temp table ``tmp_wkg_ontology`` with one row per
        (osm_key, osm_value, class_name) mapping.  Rows where
        osm_value is NULL represent key-only matches (depth-1 classes).

        The temp table lives for the duration of the DB connection
        (ON COMMIT PRESERVE ROWS, dropped at connection close).
        """
        self.ontology._ensure_index()

        cursor.execute("""
            CREATE TEMP TABLE IF NOT EXISTS tmp_wkg_ontology (
                osm_key    TEXT,
                osm_value  TEXT,
                class_name TEXT,
                depth      INT,
                superclasses TEXT[],
                wikidata_uri TEXT,
                type_key    TEXT,
                type_value  TEXT
            ) ON COMMIT PRESERVE ROWS
        """)
        cursor.execute("TRUNCATE tmp_wkg_ontology")

        rows = []
        for osm_key, value_map in self.ontology._key_value_index.items():
            for osm_value, class_name in value_map.items():
                depth = self.ontology._class_depths.get(class_name, 0)
                sup = self.ontology._class_superclasses.get(class_name, [])
                wd = self.ontology._class_wikidata.get(class_name)
                kv = self.ontology._class_to_key_value.get(class_name)
                tk, tv = kv if kv else (osm_key, osm_value)
                rows.append((osm_key, osm_value, class_name, depth,
                             sup if sup else None, wd, tk, tv))

        for osm_key, class_name in self.ontology._key_class_index.items():
            depth = self.ontology._class_depths.get(class_name, 0)
            sup = self.ontology._class_superclasses.get(class_name, [])
            wd = self.ontology._class_wikidata.get(class_name)
            tk = self.ontology._key_class_index_reverse.get(class_name, osm_key)
            rows.append((osm_key, None, class_name, depth,
                         sup if sup else None, wd, tk, None))

        # Bulk insert via execute_values for speed
        from psycopg2.extras import execute_values
        execute_values(
            cursor,
            "INSERT INTO tmp_wkg_ontology "
            "(osm_key, osm_value, class_name, depth, superclasses, "
            " wikidata_uri, type_key, type_value) VALUES %s",
            rows,
        )
        logger.info(
            "Loaded %d ontology rows into tmp_wkg_ontology",
            len(rows),
        )

    def sql_enrich_region(
        self,
        region: Optional[str] = None,
        poly_file: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        skip_enriched: bool = True,
        limit: Optional[int] = None,
    ) -> Dict:
        """SQL-side batch enrichment — replaces the Python loop entirely.

        Loads the ontology into a temp table, then runs a single
        UPDATE...FROM join that matches each entity's tags JSONB
        against the ontology.  Picks the deepest matching class per
        entity (same logic as predict_class_from_tags).

        This is ~10x faster than the Python ThreadPoolExecutor path
        for large countries because:
        - No Python per-entity loop (GIL bottleneck eliminated)
        - PostgreSQL parallel query can use multiple cores
        - Single round-trip for the UPDATE instead of bulk_update batches

        Falls back to batch_enrich_region if the ontology is not loaded.
        """
        self.ontology._ensure_index()
        if not self.ontology._index_built:
            logger.warning("sql_enrich_region: ontology not loaded, falling back")
            return self.batch_enrich_region(
                region=region, poly_file=poly_file,
                snapshot_id=snapshot_id, skip_enriched=skip_enriched,
                limit=limit,
            )

        # Spatial filtering (same logic as batch_enrich_region)
        bbox: Optional[Tuple[float, float, float, float]] = None
        if poly_file:
            from core.services.planet_init.osm_wikidata_resolver import parse_poly_bbox
            bbox = parse_poly_bbox(poly_file)
        elif region:
            bbox = resolve_country_bbox(region, None)

        from django.db import connections
        db_alias = 'vectors'
        conn = connections[db_alias]

        with conn.cursor() as cursor:
            # 1. Load ontology into temp table
            self._load_ontology_to_temp_table(cursor)

            # 2. Resolve leaf table name
            leaf = self._resolve_leaf_table(cursor, snapshot_id, region)

            # 3. Build WHERE clause for spatial/snapshot filtering
            where_clauses = []
            params = []
            if snapshot_id:
                where_clauses.append("e.snapshot_id = %s")
                params.append(snapshot_id)
            if skip_enriched:
                where_clauses.append("e.wkg_class IS NULL")
            if bbox:
                min_lon, min_lat, max_lon, max_lat = bbox
                where_clauses.append(
                    "ST_Within(e.geom, "
                    "ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
                )
                params.extend([min_lon, min_lat, max_lon, max_lat])
            where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

            # 4. Count candidates
            cursor.execute(
                f"SELECT count(*) FROM {leaf} e WHERE {where_sql}",
                params,
            )
            total = cursor.fetchone()[0]
            logger.info("SQL enrichment: %d candidate entities", total)

            if total == 0:
                self.stats = {'enriched': 0, 'failed': 0, 'skipped': 0,
                              'sparql_queries': 0, 'local_predictions': 0}
                return self.stats

            # 5. Single UPDATE...FROM join
            # For each entity, expand tags JSONB into key-value rows,
            # join against tmp_wkg_ontology, pick the deepest match.
            # Uses jsonb_each_text to expand tags.
            batch_timestamp = timezone.now()
            update_sql = f"""
                WITH matched AS (
                    SELECT
                        e.id AS entity_id,
                        o.class_name,
                        o.depth,
                        o.superclasses,
                        o.wikidata_uri,
                        o.type_key,
                        o.type_value,
                        ROW_NUMBER() OVER (
                            PARTITION BY e.id
                            ORDER BY o.depth DESC NULLS LAST
                        ) AS rn
                    FROM {leaf} e
                    CROSS JOIN LATERAL jsonb_each_text(e.tags::jsonb) AS t(osm_key, osm_value)
                    JOIN tmp_wkg_ontology o
                      ON o.osm_key = t.osm_key
                     AND (o.osm_value = t.osm_value OR o.osm_value IS NULL)
                    WHERE {where_sql}
                )
                UPDATE {leaf} e
                SET
                    wkg_class = m.class_name,
                    wkg_superclasses = m.superclasses,
                    wikidata_uri = m.wikidata_uri,
                    wkg_depth = m.depth,
                    wkg_type_key = m.type_key,
                    wkg_type_value = m.type_value,
                    wkg_enriched_at = %s
                FROM matched m
                WHERE e.id = m.entity_id AND m.rn = 1
            """
            update_params = params + [batch_timestamp]

            logger.info("SQL enrichment: running UPDATE...FROM join")
            cursor.execute(update_sql, update_params)
            enriched_count = cursor.rowcount

            # 6. Count failed (entities that matched no ontology entry)
            cursor.execute(
                f"SELECT count(*) FROM {leaf} e "
                f"WHERE {where_sql} AND e.wkg_class IS NULL",
                params,
            )
            failed_count = cursor.fetchone()[0]

        self.stats = {
            'enriched': enriched_count,
            'failed': failed_count,
            'skipped': 0,
            'sparql_queries': 0,
            'local_predictions': enriched_count,
        }
        logger.info("SQL enrichment complete: %s", self.stats)
        return self.stats

    def _resolve_leaf_table(self, cursor, snapshot_id: str, region: str) -> str:
        """Resolve the leaf partition table name for a snapshot+country.

        The leaf table follows the naming convention:
            embeddings_{snapshot}_{country_lower}

        Falls back to the root table 'semantic_search_osmentity'
        if partitioning hasn't happened yet.
        """
        if not region:
            return "semantic_search_osmentity"

        cc = region.lower()
        candidate = f"embeddings_{snapshot_id}_{cc}"

        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = %s
            )
        """, [candidate])
        exists = cursor.fetchone()[0]

        if exists:
            return candidate
        else:
            logger.warning(
                "Leaf table %s not found, falling back to root table",
                candidate,
            )
            return "semantic_search_osmentity"
