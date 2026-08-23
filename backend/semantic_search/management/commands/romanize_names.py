"""Romanize OSM entity names for cross-script similarity search.

Processes entities where name_romanized IS NULL, auto-detects the script
from the name text, romanizes it via RomanizerRegistry, and bulk-updates
the name_romanized column.

Decoupled from the country pipeline — runs independently on its own
schedule. Idempotent (only processes NULL rows). Resumable (interrupted
runs continue from where they left off).

Usage:
    python manage.py romanize_names                          # all countries
    python manage.py romanize_names --country-code KR        # one country
    python manage.py romanize_names --country-code KR --batch-size 10000
    python manage.py romanize_names --limit 1000             # for testing
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import connections, transaction

from semantic_search.services.romanizing_names.registry import RomanizerRegistry

logger = logging.getLogger(__name__)

# Default batch size for bulk updates
DEFAULT_BATCH_SIZE = 5000


class Command(BaseCommand):
    help = (
        "Romanize OSM entity names for cross-script similarity search. "
        "Processes entities where name_romanized IS NULL. "
        "Idempotent and resumable."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--country-code",
            default=None,
            help="Process one country (default: all countries with NULL name_romanized)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Batch size for bulk updates (default: {DEFAULT_BATCH_SIZE})",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum entities to process (for testing; default: no limit)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Romanize and print samples without writing to DB",
        )

    def handle(self, *args, **opts):
        country_code = opts["country_code"]
        batch_size = opts["batch_size"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        # Build the query to find entities needing romanization
        # We use raw SQL on the vectors DB for efficiency
        conn = connections["vectors"]

        # Count entities needing romanization
        count_sql = """
            SELECT COUNT(*)
            FROM semantic_search_osmentity
            WHERE name_romanized IS NULL
              AND tags ? 'name'
              AND tags->>'name' != ''
        """
        count_params = []
        if country_code:
            count_sql += " AND country_code = %s"
            count_params.append(country_code)

        with conn.cursor() as cur:
            cur.execute(count_sql, count_params)
            total = cur.fetchone()[0]

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "No entities needing romanization found."
            ))
            return

        self.stdout.write(
            f"Found {total:,} entities to romanize"
            + (f" for {country_code}" if country_code else "")
            + (" (dry run)" if dry_run else "")
        )
        self.stdout.write(
            f"Supported scripts: {RomanizerRegistry.list_supported()}"
        )

        # Process in batches
        processed = 0
        start_time = time.time()

        while True:
            if limit and processed >= limit:
                break

            fetch_limit = batch_size
            if limit and processed + fetch_limit > limit:
                fetch_limit = limit - processed

            # Fetch a batch of entities needing romanization
            fetch_sql = """
                SELECT id, tags->>'name' as name
                FROM semantic_search_osmentity
                WHERE name_romanized IS NULL
                  AND tags ? 'name'
                  AND tags->>'name' != ''
            """
            fetch_params = []
            if country_code:
                fetch_sql += " AND country_code = %s"
                fetch_params.append(country_code)
            fetch_sql += " ORDER BY id LIMIT %s"
            fetch_params.append(fetch_limit)

            with conn.cursor() as cur:
                cur.execute(fetch_sql, fetch_params)
                rows = cur.fetchall()

            if not rows:
                break

            # Romanize each name
            updates = []
            for entity_id, name in rows:
                romanized = RomanizerRegistry.auto_romanize(name)
                if romanized:
                    updates.append((romanized, entity_id))

            if not updates:
                # No romanizable names in this batch — mark them as empty
                # to avoid re-fetching them
                empty_ids = [r[0] for r in rows]
                self._mark_empty(conn, empty_ids)
                processed += len(rows)
                continue

            if dry_run:
                # Print samples
                for entity_id, name in rows[:5]:
                    romanized = RomanizerRegistry.auto_romanize(name)
                    self.stdout.write(f"  {name!r} → {romanized!r}")
                processed += len(rows)
                continue

            # Bulk UPDATE
            self._bulk_update(conn, updates)

            # Mark any entities that had no romanizable name
            updated_ids = {u[1] for u in updates}
            empty_ids = [r[0] for r in rows if r[0] not in updated_ids]
            if empty_ids:
                self._mark_empty(conn, empty_ids)

            processed += len(rows)
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            self.stdout.write(
                f"  Processed {processed:,}/{total:,} "
                f"({rate:.0f} entities/sec, {elapsed:.1f}s elapsed)"
            )

        elapsed = time.time() - start_time
        self.stdout.write(self.style.SUCCESS(
            f"Done. Romanized {processed:,} entities in {elapsed:.1f}s."
        ))

    def _bulk_update(self, conn, updates):
        """Bulk UPDATE name_romanized using a VALUES clause."""
        if not updates:
            return
        # Build a VALUES clause for the bulk update
        # Using a CTE for efficient batch update
        values_sql = ", ".join(
            "(%s, %s)" for _ in updates
        )
        params = []
        for romanized, entity_id in updates:
            params.extend([romanized, entity_id])

        sql = f"""
            UPDATE semantic_search_osmentity AS e
            SET name_romanized = v.romanized
            FROM (VALUES {values_sql}) AS v(romanized, entity_id)
            WHERE e.id = v.entity_id
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
        transaction.commit(using="vectors")

    def _mark_empty(self, conn, entity_ids):
        """Mark entities as having an empty romanized name (avoid re-fetching)."""
        if not entity_ids:
            return
        # Set name_romanized to empty string for entities with no romanizable name
        placeholders = ", ".join(["%s"] * len(entity_ids))
        sql = f"""
            UPDATE semantic_search_osmentity
            SET name_romanized = ''
            WHERE id IN ({placeholders})
        """
        with conn.cursor() as cur:
            cur.execute(sql, entity_ids)
        transaction.commit(using="vectors")
