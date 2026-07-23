import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from api.models import PolygonFile

class Command(BaseCommand):
    help = 'Scans the POLYGON_FILES_DIR and ingests .poly files into the database.'

    def handle(self, *args, **options):
        poly_files_dir = Path(settings.POLYGON_FILES_DIR)
        if not poly_files_dir.is_dir():
            self.stdout.write(self.style.ERROR(f'Polygon files directory does not exist: {poly_files_dir}'))
            return

        self.stdout.write(f'Scanning for .poly files in {poly_files_dir}...')
        ingested_count = 0
        for poly_file_path in poly_files_dir.rglob('*.poly'):
            file_path_str = str(poly_file_path.resolve())
            name = poly_file_path.stem

            try:
                _, created = PolygonFile.objects.get_or_create(
                    file_path=file_path_str,
                    defaults={
                        'name': name,
                        'region_name': name,
                    }
                )

                if created:
                    ingested_count += 1
                    self.stdout.write(self.style.SUCCESS(f'Ingested: {file_path_str}'))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error ingesting {file_path_str}: {e}'))

        self.stdout.write(self.style.SUCCESS(f'Ingestion complete. Ingested {ingested_count} new polygon files.'))
