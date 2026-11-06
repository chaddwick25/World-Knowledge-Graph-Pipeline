"""Database preflight: extensions and orphaned pg_type cleanup.

Replaces the equivalent bash in ``docker-entrypoint.sh``, which parsed
credentials by colon-joining them (breaking on a password containing a colon)
and ran the same SQL through ``psql`` with hand-built connection strings.

Runs against both configured databases.  Idempotent — safe on every boot.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import connections

logger = logging.getLogger("pipeline")

# Extensions each database needs.  `pg_trgm` and `fuzzystrmatch` arrive with
# migrations 0019/0020, so they are not created here.
REQUIRED_EXTENSIONS = {
    "default": ("postgis", "vector"),
    "vectors": ("vector",),
}

ORPHAN_TYPE_CLEANUP = """
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT t.typname, t.oid
    FROM pg_type t
    LEFT JOIN pg_class c ON c.oid = t.typrelid
    WHERE t.typtype = 'c'
      AND t.typrelid != 0
      AND c.oid IS NULL
      AND t.typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
  LOOP
    EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
    RAISE NOTICE 'Dropped orphaned type: %', r.typname;
  END LOOP;
END $$;
"""


class Command(BaseCommand):
    help = "Create required extensions and drop orphaned pg_type entries"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-cleanup",
            action="store_true",
            help="Only create extensions (skip the pg_type orphan sweep).",
        )

    def handle(self, *args, **options):
        for alias, extensions in REQUIRED_EXTENSIONS.items():
            connection = connections[alias]
            self.stdout.write(f"db_preflight: {alias} ({connection.settings_dict['NAME']})")

            with connection.cursor() as cursor:
                for extension in extensions:
                    cursor.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
                    self.stdout.write(f"  extension ok: {extension}")

                if options["skip_cleanup"]:
                    continue
                try:
                    cursor.execute(ORPHAN_TYPE_CLEANUP)
                except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                    logger.warning(
                        "pg_type cleanup failed on %s: %s", alias, exc
                    )

        self.stdout.write(self.style.SUCCESS("db_preflight: done"))
