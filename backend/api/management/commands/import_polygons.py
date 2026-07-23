from django.core.management.base import BaseCommand
from django.conf import settings
from api.models import PolygonFile
import os

class Command(BaseCommand):
    help = 'Scans the POLYGON_FILES_DIR and populates the PolygonFile table.'

    def handle(self, *args, **options):
        polygon_dir = settings.POLYGON_FILES_DIR
        if not os.path.isdir(polygon_dir):
            self.stdout.write(self.style.ERROR(f'Polygon directory not found at: {polygon_dir}'))
            return

        for root, _, files in os.walk(polygon_dir):
            for file in files:
                if file.endswith('.poly'):
                    file_path = os.path.join(root, file)
                    name = os.path.splitext(file)[0]
                    region_name = os.path.basename(os.path.dirname(file_path))

                    try:
                        poly_file, created = PolygonFile.objects.get_or_create(
                            file_path=file_path,
                            defaults={
                                'name': name,
                                'region_name': region_name,
                            }
                        )

                        if created:
                            self.stdout.write(self.style.SUCCESS(f'Successfully imported: {name}'))
                        else:
                            self.stdout.write(self.style.WARNING(f'Skipping existing polygon: {name}'))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f'Error importing {name}: {e}'))
