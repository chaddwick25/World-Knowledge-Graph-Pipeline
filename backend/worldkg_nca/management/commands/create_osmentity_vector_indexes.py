from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = (
        "Create HNSW indexes on semantic_search_osmentity leaf partitions.\n\n"
        "Default (no args): global mode — creates HNSW on every leaf that is "
        "missing one.\n"
        "With --country + --snapshot: per-leaf mode — creates HNSW only on "
        "embeddings_{snapshot}_{country}.  Per-leaf mode is what Step 1 uses "
        "so the rebuild cost scales with the country being upserted, not the "
        "total rows across all countries."
    )

    HNSW_M = 16
    HNSW_EF_CONSTRUCTION = 128

    # Post-build verification: fraction of self-probe anchors whose
    # embedding cluster must be reachable via ANN (top-1 distance ~0).
    # Scattered misses mean the index is silently missing clusters and
    # needs a REINDEX.
    VERIFY_THRESHOLD = 0.99
    # Reachability tolerance: covers float noise from the vector::text
    # roundtrip. Distance-0 embedding twins and the anchor itself both
    # land at ~0; a genuinely unreachable cluster returns its nearest
    # neighbour's distance, which is far larger.
    VERIFY_EPSILON = 0.0001
    # Probe at a higher ef_search than the runtime default (40) so misses
    # reflect index completeness, not recall-at-ef.
    VERIFY_EF_SEARCH = 200

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", type=str, default=None,
            help="Country code. If set with --snapshot, create HNSW only on "
                 "the specified leaf.",
        )
        parser.add_argument(
            "--snapshot", type=str, default=None,
            help="Snapshot date. If set with --country, create HNSW only on "
                 "the specified leaf.",
        )
        parser.add_argument(
            "--categories", type=str, default=None,
            help="Comma-separated OSM tag keys for value-vocabulary "
                 "indexes (per-leaf mode only). Defaults to "
                 "GENERIC_AMENITY_KEYS (the guaranteed Step 6 slice). "
                 "Keys are validated against the executor constants in "
                 "semantic_search/services/query_executor_service/"
                 "_constants.py.",
        )
        parser.add_argument(
            "--verify", action="store_true",
            help="Self-probe HNSW indexes instead of creating them: sample "
                 "rows per leaf, check each is reachable via ANN (its own "
                 "embedding as the query). Per-leaf scope works the same "
                 "as index creation.",
        )
        parser.add_argument(
            "--verify-sample", type=int, default=50,
            help="Rows probed per leaf-column under --verify (default 50).",
        )
        parser.add_argument(
            "--verify-ef", type=int, default=None,
            help="hnsw.ef_search for the probe session (default "
                 f"{self.VERIFY_EF_SEARCH}).",
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Under --verify: exit non-zero if any probed leaf-column "
                 "falls below the self-probe threshold.",
        )

    def handle(self, *args, **options):
        country = options.get("country")
        snapshot = options.get("snapshot")

        if options.get("verify"):
            self._run_verify(country, snapshot, options)
            return

        categories = self._parse_categories(
            options.get("categories"), country, snapshot,
        )
        if categories is None:
            return

        if country and snapshot:
            leaf_name = f"embeddings_{snapshot}_{country.lower()}"
            self.stdout.write(self.style.WARNING(
                f"Per-leaf mode: creating all indexes on {leaf_name} only..."
            ))
            created = self._create_leaf_indexes(leaf_name, categories)
            self.stdout.write(self.style.SUCCESS(
                f"Created {created} index(es) on leaf {leaf_name}."
            ))
            return

        if country or snapshot:
            self.stderr.write(self.style.ERROR(
                "Both --country and --snapshot must be provided together "
                "(or neither for global mode)."
            ))
            return

        # Global mode: existing behavior (all leaves missing HNSW)
        self.stdout.write(self.style.WARNING(
            "Global mode: creating HNSW indexes on semantic_search_osmentity "
            "(vectors DB)..."
        ))
        created = self._create_all_missing_hnsw()
        self.stdout.write(self.style.SUCCESS(
            f"Created {created} HNSW index(es) on leaf partitions."
        ))

    # ------------------------------------------------------------------ #
    # Per-leaf mode
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_categories():
        """The guaranteed Step 6 slice: GENERIC_AMENITY_KEYS.

        These are the OSM tag keys the parser can emit as generic OBJECT
        vocabulary — exactly the bounded value sets a correction or
        suggestion path can enumerate. Cost measured on the 2025_12_31
        maintained leaves (ie/jm/mx): ~1.1M index entries total, ~60%
        of which is amenity alone.
        """
        from semantic_search.services.query_executor_service._constants import (
            GENERIC_AMENITY_KEYS,
        )
        return tuple(GENERIC_AMENITY_KEYS)

    def _parse_categories(self, raw, country, snapshot):
        """Resolve the --categories flag into a validated key tuple.

        Default is GENERIC_AMENITY_KEYS: the parser's generic OBJECT
        vocabulary, which is where vocabulary enumeration has readers
        (QueryCorrectionService.correct_amenity is the reference
        consumer). The allowed set comes from the executor constants
        (DEC-007), never a hand-maintained list — the same source the
        opt-in catalog derives from, so a key the executor cannot emit
        can never get an index. Keys land in raw SQL identifiers, so the
        whitelist is also the injection guard.
        """
        from semantic_search.services.query_executor_service._constants import (
            CATEGORY_TAG_TARGETS, GENERIC_AMENITY_KEYS,
        )
        allowed = set(GENERIC_AMENITY_KEYS) | {
            v[0] for v in CATEGORY_TAG_TARGETS.values()
        }
        requested = (
            tuple(k.strip().lower() for k in raw.split(",") if k.strip())
            if raw else self._default_categories()
        )
        if not requested:
            self.stderr.write(self.style.ERROR("--categories is empty."))
            return None
        if raw and not (country and snapshot):
            self.stderr.write(self.style.ERROR(
                "--categories only applies in per-leaf mode "
                "(--country + --snapshot)."
            ))
            return None
        invalid = [k for k in requested if k not in allowed]
        if invalid:
            self.stderr.write(self.style.ERROR(
                f"Unknown categor{'y' if len(invalid) == 1 else 'ies'} "
                f"{invalid!r}. Valid keys: {sorted(allowed)}"
            ))
            return None
        return requested

    def _create_leaf_indexes(self, leaf_name: str,
                             categories=None) -> int:
        """Rebuild ALL indexes dropped by drop_osmentity_vector_indexes per-leaf mode.

        Called after a bulk upsert completes.  Rebuilds:
          1. HNSW vector indexes (gv_tags_embedding, gv_nle_embedding)
          2. Value-vocabulary partial indexes, one per category key
             (GENERIC_AMENITY_KEYS by default — the guaranteed Step 6
             slice)
          3. GIST geometry index (geom)
          4. Auxiliary B-tree indexes (osm_type+osm_id, wikidata_uri, wkg_class)

        Uses CONCURRENTLY for HNSW and vocab indexes (can't hold
        ShareLock on large data); plain CREATE INDEX for the B-tree/GIST
        indexes (faster, no transaction issues).
        """
        if categories is None:
            categories = self._default_categories()
        conn = connections['vectors']
        created = 0

        # ---- 1. HNSW vector indexes (CONCURRENTLY required) ----
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cursor.execute(
                    """
                    SELECT a.attname AS column_name
                    FROM pg_class t
                    JOIN pg_namespace n ON t.relnamespace = n.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid
                    JOIN pg_type ty ON a.atttypid = ty.oid
                    WHERE t.relkind = 'r'
                      AND n.nspname = 'public'
                      AND t.relname = %s
                      AND a.attname IN ('gv_tags_embedding', 'gv_nle_embedding')
                      AND ty.typname = 'vector'
                      AND NOT EXISTS (
                        SELECT 1 FROM pg_indexes i
                        WHERE i.tablename = t.relname
                          AND i.indexdef LIKE '%%hnsw%%'
                          AND i.indexdef LIKE '%%' || a.attname || '%%'
                      )
                    ORDER BY a.attname;
                    """,
                    [leaf_name],
                )
                missing_hnsw = cursor.fetchall()

            for (col_name,) in missing_hnsw:
                idx_name = f"idx_{leaf_name}_{col_name}_hnsw"
                self.stdout.write(f"  Creating HNSW index {idx_name}...")
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name}
                        ON {leaf_name}
                        USING hnsw ({col_name} vector_cosine_ops)
                        WITH (m = {self.HNSW_M},
                              ef_construction = {self.HNSW_EF_CONSTRUCTION});
                    """)
                self.stdout.write(f"    Created {idx_name}")
                created += 1

            # ---- 2. Value-vocabulary indexes (CONCURRENTLY) ----
            # Partial composite expression index per category key:
            #   ((tags->>'<key>'), snapshot_id, country_code)
            #   WHERE tags ? '<key>'
            # turns the DISTINCT tags->>'<key>' census into an
            # index-only scan over the key's tagged rows instead of a
            # heap fetch of every match (correct_amenity's 1.3M-row
            # stall on the PL leaf is the reference incident).
            for key in categories:
                idx_name = f"idx_{leaf_name}_{key}_vocab"
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                        "WHERE indexname = %s);",
                        [idx_name],
                    )
                    if cursor.fetchone()[0]:
                        continue
                    self.stdout.write(f"  Creating vocab index {idx_name}...")
                    cursor.execute(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name}
                        ON {leaf_name} ((tags->>'{key}'), snapshot_id, country_code)
                        WHERE tags ? '{key}';
                    """)
                    self.stdout.write(f"    Created {idx_name}")
                    created += 1
        finally:
            conn.set_autocommit(old_autocommit)

        # ---- 3. GIST geometry index + auxiliary B-tree indexes ----
        # These are plain CREATE INDEX (no CONCURRENTLY needed — no live
        # readers during Step 1 rebuild window).
        aux_indexes = [
            (
                f"idx_{leaf_name}_geom",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_geom "
                f"ON {leaf_name} USING gist (geom);",
            ),
            (
                f"idx_{leaf_name}_osm",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_osm "
                f"ON {leaf_name} USING btree (osm_type, osm_id);",
            ),
            # osm_id-only lookup index: enrichment/context queries filter
            # `osm_id__in` without osm_type, which cannot seek the
            # (osm_type, osm_id) composite and degenerates to a full
            # index scan (~0.5-8s on an IE-class leaf vs ~1ms seeks).
            (
                f"idx_{leaf_name}_osm_id",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_osm_id "
                f"ON {leaf_name} USING btree (osm_id);",
            ),
            (
                f"idx_{leaf_name}_wikidata",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_wikidata "
                f"ON {leaf_name} USING btree (wikidata_uri);",
            ),
            (
                f"idx_{leaf_name}_wkg_class",
                f"CREATE INDEX IF NOT EXISTS idx_{leaf_name}_wkg_class "
                f"ON {leaf_name} USING btree (wkg_class);",
            ),
        ]
        with conn.cursor() as cursor:
            for idx_name, idx_sql in aux_indexes:
                # Only create if missing.
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = %s);",
                    [idx_name],
                )
                if not cursor.fetchone()[0]:
                    self.stdout.write(f"  Creating index {idx_name}...")
                    cursor.execute(idx_sql)
                    self.stdout.write(f"    Created {idx_name}")
                    created += 1

        return created

    # kept for backward compatibility (global mode calls this)
    def _create_leaf_hnsw(self, leaf_name: str) -> int:
        """Alias: create only HNSW indexes (used by global mode)."""
        return self._create_leaf_indexes(leaf_name)

    # ------------------------------------------------------------------ #
    # Global mode (existing behavior, refactored into a method)
    # ------------------------------------------------------------------ #
    def _create_all_missing_hnsw(self) -> int:
        conn = connections['vectors']
        # CONCURRENTLY requires autocommit (no transaction block).
        old_autocommit = conn.get_autocommit()
        conn.set_autocommit(True)
        try:
            with conn.cursor() as cursor:
                # Ensure pgvector extension is available in this database
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

                # Create HNSW indexes on all leaf partitions that have
                # vector columns but no HNSW index yet.
                cursor.execute("""
                    SELECT t.relname AS table_name,
                           a.attname AS column_name
                    FROM pg_class t
                    JOIN pg_namespace n ON t.relnamespace = n.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid
                    JOIN pg_type ty ON a.atttypid = ty.oid
                    WHERE t.relkind = 'r'
                      AND n.nspname = 'public'
                      AND t.relname LIKE 'embeddings\\_%' ESCAPE '\\'
                      AND a.attname IN ('gv_tags_embedding', 'gv_nle_embedding')
                      AND ty.typname = 'vector'
                      AND NOT EXISTS (
                        SELECT 1 FROM pg_indexes i
                        WHERE i.tablename = t.relname
                          AND i.indexdef LIKE '%hnsw%'
                          AND i.indexdef LIKE '%' || a.attname || '%'
                      )
                    ORDER BY t.relname, a.attname;
                """)
                missing = cursor.fetchall()

            # Create each HNSW index in its own autocommit statement
            created = 0
            for table_name, col_name in missing:
                idx_name = f"idx_{table_name}_{col_name}_hnsw"
                self.stdout.write(f"  Creating HNSW index {idx_name} on {table_name}.{col_name}...")
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name}
                        ON {table_name}
                        USING hnsw ({col_name} vector_cosine_ops)
                        WITH (m = {self.HNSW_M},
                              ef_construction = {self.HNSW_EF_CONSTRUCTION});
                    """)
                self.stdout.write(f"    Created {idx_name}")
                created += 1
        finally:
            conn.set_autocommit(old_autocommit)
        return created

    # ------------------------------------------------------------------ #
    # Verify mode (--verify): ANN self-probe for index completeness
    # ------------------------------------------------------------------ #

    def _run_verify(self, country, snapshot, options):
        """Self-probe HNSW indexes on leaf partitions.

        Samples rows per (leaf, column) pair that carries an HNSW index,
        then queries ANN with each row's own embedding — a complete index
        reaches a distance-~0 neighbour (the anchor or an identical-
        embedding twin).  Misses mean whole embedding clusters are
        silently absent; REINDEX is the remedy.

        Reachability (not anchor membership) is the check: gv_tags
        embeddings are heavily duplicated — identical tag sets produce
        identical vectors — so an anchor can sit under thousands of
        distance-0 twins and stay out of any top-K even when its
        cluster is fully indexed (this explains the apparent 2026-10-23
        "MX missing rows" incident).

        Scoped like index creation: --country + --snapshot probes one
        leaf; bare --verify censuses every leaf with an HNSW index.
        """
        sample = options.get("verify_sample") or 50
        ef_search = options.get("verify_ef") or self.VERIFY_EF_SEARCH
        strict = options.get("strict")

        pairs = self._hnsw_leaf_columns(country, snapshot)
        if not pairs:
            scope = (
                f"embeddings_{snapshot}_{country.lower()}"
                if country and snapshot else "any leaf"
            )
            msg = f"no HNSW indexes found on {scope} — nothing to verify"
            if strict:
                from django.core.management.base import CommandError
                raise CommandError(msg)
            self.stderr.write(self.style.WARNING(msg))
            return

        conn = connections['vectors']
        with conn.cursor() as cursor:
            cursor.execute(f"SET hnsw.ef_search = {int(ef_search)};")

        self.stdout.write(self.style.WARNING(
            f"Verifying {len(pairs)} leaf-column index(es): "
            f"{sample} anchors each, ef_search={ef_search}, "
            f"threshold={self.VERIFY_THRESHOLD:.0%}"
        ))

        total_found = total_probed = 0
        below_threshold = []
        for leaf_name, col_name in pairs:
            found, probed, missing = self._probe_leaf_column(
                conn, leaf_name, col_name, sample,
            )
            total_found += found
            total_probed += probed
            if probed == 0:
                self.stdout.write(
                    f"  {leaf_name}.{col_name}: no non-null rows to probe"
                )
                continue
            rate = found / probed
            line = (
                f"  {leaf_name}.{col_name}: {found}/{probed} "
                f"reachable ({rate:.1%})"
            )
            if missing:
                line += f" — missed: {', '.join(missing[:5])}"
                if len(missing) > 5:
                    line += f" (+{len(missing) - 5} more)"
            if rate < self.VERIFY_THRESHOLD:
                below_threshold.append(f"{leaf_name}.{col_name}")
                self.stderr.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)

        if total_probed == 0:
            self.stderr.write(self.style.WARNING(
                "No rows probed — nothing to verify."
            ))
            return

        rate = total_found / total_probed
        self.stdout.write(self.style.SUCCESS(
            f"Self-probe: {total_found}/{total_probed} reachable ({rate:.1%})"
        ))
        if strict and below_threshold:
            from django.core.management.base import CommandError
            raise CommandError(
                f"verify failed: {len(below_threshold)} leaf-column(s) below "
                f"{self.VERIFY_THRESHOLD:.0%} self-probe threshold: "
                + ", ".join(below_threshold)
            )

    def _hnsw_leaf_columns(self, country, snapshot):
        """(leaf, column) pairs that carry an HNSW index."""
        where = "t.relname LIKE 'embeddings\\_%' ESCAPE '\\'"
        params = []
        if country and snapshot:
            where = "t.relname = %s"
            params = [f"embeddings_{snapshot}_{country.lower()}"]
        conn = connections['vectors']
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT t.relname AS table_name,
                       a.attname AS column_name
                FROM pg_class t
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_attribute a ON a.attrelid = t.oid
                JOIN pg_type ty ON a.atttypid = ty.oid
                WHERE t.relkind = 'r'
                  AND n.nspname = 'public'
                  AND {where}
                  AND a.attname IN ('gv_tags_embedding', 'gv_nle_embedding')
                  AND ty.typname = 'vector'
                  AND EXISTS (
                    SELECT 1 FROM pg_indexes i
                    WHERE i.tablename = t.relname
                      AND i.indexdef LIKE '%%hnsw%%'
                      AND i.indexdef LIKE '%%' || a.attname || '%%'
                  )
                ORDER BY t.relname, a.attname;
                """,
                params,
            )
            return cursor.fetchall()

    def _probe_leaf_column(self, conn, leaf_name, col_name, sample):
        """Sample rows and check each embedding cluster is reachable.

        For each anchor, asks ANN for the single nearest row and counts
        the anchor reachable when that distance is <= VERIFY_EPSILON.
        Anchor membership in a top-K is deliberately not checked:
        gv_tags embeddings are heavily duplicated (identical tag sets
        produce identical vectors), so an anchor can sit under
        thousands of distance-0 twins and fall outside any finite
        top-K while its cluster is perfectly indexed — which is all
        the search path needs.

        Returns (reachable, probed, missed_keys_with_dist).
        """
        # TABLESAMPLE SYSTEM reads ~1% of pages — cheap on 45 GB leaves and
        # still page-scattered.  Small leaves may sample < sample rows or
        # nothing; the random() fallback covers that case.
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT osm_type, osm_id, {col_name}::text
                FROM {leaf_name} TABLESAMPLE SYSTEM (1)
                WHERE {col_name} IS NOT NULL
                LIMIT %s;
                """,
                [sample],
            )
            rows = cursor.fetchall()
            if not rows:
                cursor.execute(
                    f"""
                    SELECT osm_type, osm_id, {col_name}::text
                    FROM {leaf_name}
                    WHERE {col_name} IS NOT NULL
                    ORDER BY random() LIMIT %s;
                    """,
                    [sample],
                )
                rows = cursor.fetchall()

        found = 0
        missing = []
        for osm_type, osm_id, vec in rows:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {col_name} <=> %s::vector AS d
                    FROM {leaf_name}
                    ORDER BY d
                    LIMIT 1;
                    """,
                    [vec],
                )
                nearest = cursor.fetchone()
            if nearest and nearest[0] is not None \
                    and nearest[0] <= self.VERIFY_EPSILON:
                found += 1
            else:
                d = nearest[0] if nearest else None
                d_str = "none" if d is None else f"{d:.4f}"
                missing.append(f"{osm_type}/{osm_id} d={d_str}")
        return found, len(rows), missing
