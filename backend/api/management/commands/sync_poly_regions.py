import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.conf import settings
from api.models import RegionHierarchy, PbfFile
from extraction.services.regional_path_service import normalize_continent_slug

class Command(BaseCommand):
    help = 'Sync polygon file hierarchy with RegionHierarchy and RegionalExtractionState models'

    def handle(self, *args, **options):
        folder_path = Path(os.getenv('FOLDER_PATH') or settings.POLYGON_FILES_DIR)
        source_planet_path = os.getenv('SOURCE_PBF_PATH') or os.getenv('PLANET_OSM_FILE_PATH')

        if not folder_path.exists():
            self.stdout.write(self.style.ERROR(f"Folder path does not exist: {folder_path}"))
            return

        if not source_planet_path:
            self.stdout.write(self.style.WARNING("SOURCE_PBF_PATH not set. State will be registered without planet reference."))

        self.stdout.write(f"Syncing hierarchy from: {folder_path}")
        self.sync_hierarchy(folder_path, source_planet_path)
        self.stdout.write(self.style.SUCCESS("✓ Polygon hierarchy sync complete."))

    def sync_hierarchy(self, folder_path, source_planet_path):
        EXCLUDED_DIRS = {'merge'}
        CONTINENT_PRIORITY = [
            'europe', 'africa', 'asia', 'north-america', 'south-america',
            'central-america', 'oceania', 'russia', 'antarctica',
        ]

        def sync_directory(current_path, parent_obj=None):
            from extraction.models import PolygonFile, RegionalExtractionState
            for item in sorted(current_path.iterdir()):
                if item.is_dir():
                    if item.name in EXCLUDED_DIRS:
                        continue
                    # Normalize continent names to use underscores (database convention)
                    normalized_name = normalize_continent_slug(item.name) if parent_obj is None else item.name
                    # Determine region_type based on parent
                    region_type = RegionHierarchy.RegionType.CONTINENT if parent_obj is None else RegionHierarchy.RegionType.COUNTRY
                    region_obj, created = RegionHierarchy.objects.get_or_create(
                        name=normalized_name,
                        parent=parent_obj,
                        defaults={'poly_file_path': None, 'region_type': region_type}
                    )
                    # Update region_type if it wasn't set
                    if not created and not region_obj.region_type:
                        region_obj.region_type = region_type
                        region_obj.save(update_fields=['region_type'])
                    sync_directory(item, parent_obj=region_obj)

                elif item.is_file() and item.suffix == '.poly':
                    resolved_path = str(item.resolve())
                    
                    # Special Rule: If the file name matches the parent folder name (e.g. africa/africa.poly),
                    # we use the parent object itself instead of creating a child.
                    if parent_obj and item.stem.lower() == parent_obj.name.lower():
                        region_obj = parent_obj
                        if not region_obj.poly_file_path:
                            region_obj.poly_file_path = resolved_path
                        # Ensure region_type is set
                        if not region_obj.region_type:
                            region_obj.region_type = RegionHierarchy.RegionType.CONTINENT
                        region_obj.save(update_fields=['poly_file_path', 'region_type'])
                    else:
                        # Determine region_type based on parent
                        region_type = RegionHierarchy.RegionType.CONTINENT if parent_obj is None else RegionHierarchy.RegionType.COUNTRY
                        region_obj, created = RegionHierarchy.objects.get_or_create(
                            name=item.stem,
                            parent=parent_obj,
                            defaults={'poly_file_path': resolved_path, 'region_type': region_type}
                        )
                        
                        if not created:
                            if region_obj.poly_file_path != resolved_path:
                                region_obj.poly_file_path = resolved_path
                            if not region_obj.region_type:
                                region_obj.region_type = region_type
                            region_obj.save(update_fields=['poly_file_path', 'region_type'])

                    # Link PolygonFile
                    poly_file_obj, p_created = PolygonFile.objects.get_or_create(
                        file_path=resolved_path,
                        defaults={'name': item.stem, 'region_name': item.stem}
                    )
                    if region_obj.polygon_file_id != poly_file_obj.id:
                        region_obj.polygon_file = poly_file_obj
                        region_obj.save(update_fields=['polygon_file'])
                    
                    # Initialize RegionalExtractionState
                    # A continent is either a root .poly file OR a .poly file inside a root folder that matches the folder name
                    is_continent = (parent_obj is None) or (parent_obj.parent is None and item.stem.lower() == parent_obj.name.lower())
                    r_type = RegionalExtractionState.RegionType.CONTINENT if is_continent else RegionalExtractionState.RegionType.COUNTRY
                    
                    RegionalExtractionState.objects.get_or_create(
                        source_planet_path=source_planet_path,
                        poly_file=poly_file_obj,
                        defaults={
                            'region_name': item.stem,
                            'region_type': r_type,
                            'status': RegionalExtractionState.StateStatus.PENDING
                        }
                    )

        sync_directory(folder_path)
        
        # Deduplication logic
        continents = RegionHierarchy.objects.filter(region_type=RegionHierarchy.RegionType.CONTINENT)
        duplicates = (
            RegionHierarchy.objects
            .filter(parent__in=continents)
            .values('name')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
        )

        for dup in duplicates:
            name = dup['name']
            entries = list(
                RegionHierarchy.objects
                .filter(name=name, parent__in=continents)
                .select_related('parent')
            )

            def sort_key(entry):
                has_path = 0 if entry.poly_file_path else 1
                continent_rank = (
                    CONTINENT_PRIORITY.index(entry.parent.name)
                    if entry.parent and entry.parent.name in CONTINENT_PRIORITY
                    else len(CONTINENT_PRIORITY)
                )
                return (has_path, continent_rank)

            entries.sort(key=sort_key)
            for entry in entries[1:]:
                entry.delete()
