from django.core.management.base import BaseCommand
from django.conf import settings
from pathlib import Path
import os
import shutil


class Command(BaseCommand):
    help = 'Verify all configured file paths exist and are accessible'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Exit with error code if any required path is missing',
        )

    def handle(self, *args, **options):
        strict = options.get('strict', False)

        paths_to_check = {
            'PLANET_OSM_FILE_PATH': {
                'value': getattr(settings, 'PLANET_OSM_FILE_PATH', None),
                'required': False,
                'description': 'Planet OSM source file',
            },
            'POLYGON_FILES_DIR': {
                'value': getattr(settings, 'POLYGON_FILES_DIR', None),
                'required': True,
                'description': 'Polygon files directory',
            },
            'PBF_CACHE_DIR': {
                'value': getattr(settings, 'PBF_CACHE_DIR', None),
                'required': False,
                'description': 'PBF cache directory (disk cache for large PBFs)',
            },
            'ASSET_BUNDLES_DIR': {
                'value': getattr(settings, 'ASSET_BUNDLES_DIR', None),
                'required': False,
                'description': 'Asset bundles directory',
            },
            'COLD_STORAGE_BASE_DIR': {
                'value': getattr(settings, 'COLD_STORAGE_BASE_DIR', None),
                'required': False,
                'description': 'Base directory for cold storage data',
            },
            'CORPUS_DIR': {
                'value': getattr(settings, 'CORPUS_DIR', None),
                'required': False,
                'description': 'FastText corpus directory',
            },
            'OSM_WIKIDATA_EXTRACTIONS_DIR': {
                'value': getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', None),
                'required': False,
                'description': 'OSM-Wikidata extractions root directory',
            },
            'OSM_CONTINENTS_OUTPUT_DIR': {
                'value': getattr(settings, 'OSM_CONTINENTS_OUTPUT_DIR', None),
                'required': False,
                'description': 'Output directory for continental PBF shards',
            },
            'OSM_WIKIDATA_TEMP_DIR': {
                'value': getattr(settings, 'OSM_WIKIDATA_TEMP_DIR', None),
                'required': False,
                'description': 'Temp directory for osmium-based Wikidata processing',
            },
            'PREPROCESSED_DIR': {
                'value': getattr(settings, 'PREPROCESSED_DIR', None),
                'required': False,
                'description': 'Preprocessed continent/country PBF output directory',
            },
            'EMBEDDINGS_ROOT': {
                'value': getattr(settings, 'EMBEDDINGS_ROOT', None),
                'required': False,
                'description': 'GeoVectors embeddings root',
            },
            'GRAPH_ASSETS_DIR': {
                'value': getattr(settings, 'GRAPH_ASSETS_DIR', None),
                'required': False,
                'description': 'Graph assets directory (GNN/KG artifacts)',
            },
            'FASTTEXT_MODEL_PATH': {
                'value': getattr(settings, 'FASTTEXT_MODEL_PATH', None),
                'required': False,
                'description': 'FastText base model binary',
            },
            'FASTTEXT_TUNED_DIR': {
                'value': getattr(settings, 'FASTTEXT_TUNED_DIR', None),
                'required': False,
                'description': 'Directory for fine-tuned FastText models',
            },
            'OSMIUM_BINARY_PATH': {
                'value': getattr(settings, 'OSMIUM_BINARY_PATH', None),
                'required': True,
                'description': 'Osmium binary executable',
            },
        }

        self.stdout.write('')
        self.stdout.write('=' * 65)
        self.stdout.write('  Path Verification Report')
        self.stdout.write('=' * 65)

        all_valid = True
        warnings = 0

        # Paths that should be treated as directories we are allowed to auto-create
        auto_create_dirs = {
            'POLYGON_FILES_DIR',
            'PBF_CACHE_DIR',
            'ASSET_BUNDLES_DIR',
            'OSM_WIKIDATA_EXTRACTIONS_DIR',
            'OSM_CONTINENTS_OUTPUT_DIR',
            'OSM_WIKIDATA_TEMP_DIR',
            'PREPROCESSED_DIR',
            'FASTTEXT_TUNED_DIR',
            # Cold storage roots/derived directories
            'COLD_STORAGE_BASE_DIR',
            'CORPUS_DIR',
            'GRAPH_ASSETS_DIR',
            'EMBEDDINGS_ROOT',
        }

        for name, info in paths_to_check.items():
            path_val = info['value']
            required = info['required']
            desc = info['description']

            if path_val is None:
                if required:
                    self.stdout.write(
                        self.style.ERROR(f'  ✗ {name}: NOT CONFIGURED ({desc})')
                    )
                    all_valid = False
                else:
                    self.stdout.write(
                        self.style.WARNING(f'  ⚠ {name}: Not set (optional - {desc})')
                    )
                    warnings += 1
                continue

            path_obj = Path(path_val)

            if name == 'OSMIUM_BINARY_PATH':
                if path_obj.exists() and path_obj.is_file():
                    self.stdout.write(
                        self.style.SUCCESS(f'  ✓ {name}: {path_val}')
                    )
                    # Check if executable
                    if os.access(str(path_obj), os.X_OK):
                        self.stdout.write(
                            self.style.SUCCESS(f'    └─ Executable: YES')
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(f'    └─ Executable: NO (check permissions)')
                        )
                        warnings += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(f'  ✗ {name}: {path_val} (NOT FOUND)')
                    )
                    all_valid = False
            elif path_obj.exists():
                if path_obj.is_dir():
                    item_count = sum(1 for _ in path_obj.rglob('*') if _.is_file())
                    self.stdout.write(
                        self.style.SUCCESS(f'  ✓ {name}: {path_val}')
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f'    └─ Contains {item_count} files')
                    )
                else:
                    size_mb = path_obj.stat().st_size / (1024 * 1024)
                    self.stdout.write(
                        self.style.SUCCESS(f'  ✓ {name}: {path_val}')
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f'    └─ Size: {size_mb:.1f} MB')
                    )
            else:
                # Attempt to auto-create directories for known directory settings
                if name in auto_create_dirs:
                    try:
                        path_obj.mkdir(parents=True, exist_ok=True)
                        self.stdout.write(
                            self.style.SUCCESS(f'  ✓ {name}: {path_val} (created)')
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(f'  ✗ {name}: {path_val} (failed to create: {e})')
                        )
                        all_valid = False
                else:
                    if required:
                        self.stdout.write(
                            self.style.ERROR(f'  ✗ {name}: {path_val} (NOT FOUND)')
                        )
                        all_valid = False
                    else:
                        self.stdout.write(
                            self.style.WARNING(f'  ⚠ {name}: {path_val} (not found, optional)')
                        )
                        warnings += 1

        self.stdout.write('')
        self.stdout.write('-' * 65)

        # Database connectivity check
        self.stdout.write('')
        self.stdout.write('  Database Connectivity:')
        db_ok = True
        for db_alias in settings.DATABASES:
            db_config = settings.DATABASES[db_alias]
            db_name = db_config.get('NAME', 'unknown')
            db_host = db_config.get('HOST', 'localhost')
            db_port = db_config.get('PORT', 'default')

            try:
                from django.db import connections
                conn = connections[db_alias]
                conn.ensure_connection()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  ✓ {db_alias}: {db_name} @ {db_host}:{db_port}'
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f'  ✗ {db_alias}: {db_name} @ {db_host}:{db_port} - {e}'
                    )
                )
                db_ok = False
                all_valid = False

        # Strict mode auto-fix: polygons + country profiles
        if strict and all_valid:
            self.stdout.write('')
            self.stdout.write('  Strict mode: running auto-fix steps (polygons + country profiles)...')
            try:
                self._run_strict_auto_fixes()
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'  ✗ Strict auto-fix failed: {e}')
                )
                all_valid = False

        self.stdout.write('')
        self.stdout.write('=' * 65)

        if all_valid and warnings == 0:
            self.stdout.write(
                self.style.SUCCESS('  ✓ All paths and connections verified!')
            )
        elif all_valid:
            self.stdout.write(
                self.style.WARNING(
                    f'  ⚠ Passed with {warnings} warning(s) (optional paths missing)'
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR('  ✗ Verification FAILED - required paths missing')
            )

        self.stdout.write('=' * 65)
        self.stdout.write('')

        if strict and not all_valid:
            raise SystemExit(1)

    def _run_strict_auto_fixes(self):
        """Run post-verification auto-fix steps for strict mode.

        - Ensure POLYGON_FILES_DIR is populated with Geofabrik .poly files
          when the directory is new/empty.
        - Run CountryPipelineProfile sync to align SQL metadata with
          embeddings + overrides.
        """

        from django.core.management import call_command
        from extraction.services.geofabrik_poly_service import download_all_geofabrik_polygons

        polygon_dir = getattr(settings, 'POLYGON_FILES_DIR', None)
        if polygon_dir:
            path_obj = Path(polygon_dir)

            existing_poly_files = []
            if path_obj.exists():
                existing_poly_files = list(path_obj.rglob('*.poly'))

            if not existing_poly_files:
                # Reset directory to ensure a clean, consistent polygon tree
                if path_obj.exists():
                    shutil.rmtree(path_obj, ignore_errors=False)

                path_obj.mkdir(parents=True, exist_ok=True)

                self.stdout.write('')
                self.stdout.write(self.style.MIGRATE_HEADING('  Populating POLYGON_FILES_DIR with Geofabrik polygons...'))

                stats = download_all_geofabrik_polygons(base_dir=str(path_obj))
                msg = (
                    '  ✓ Polygon download complete: '
                    f"{stats.get('downloaded', 0)} downloaded, "
                    f"{stats.get('skipped', 0)} skipped, "
                    f"{stats.get('not_found', 0)} not found, "
                    f"{stats.get('errors', 0)} errors"
                )
                # If we attempted a fresh bootstrap and everything failed, treat this
                # as a strict-mode error so the user can investigate.
                if stats.get('downloaded', 0) == 0 and stats.get('errors', 0) > 0:
                    self.stdout.write(self.style.ERROR(msg))
                    raise RuntimeError('Polygon download failed: see logs above for details')
                else:
                    self.stdout.write(self.style.SUCCESS(msg))

            # Warn about mixed hyphen/underscore continent names that can
            # confuse RegionHierarchy and planet initialization.
            if path_obj.exists():
                top_level_polys = {p.stem for p in path_obj.glob('*.poly')}

                def _mixed_name_pairs(names):
                    pairs = []
                    for name in sorted(names):
                        if '_' in name:
                            hyphen = name.replace('_', '-')
                            if hyphen in names:
                                pairs.append((name, hyphen))
                    return pairs

                mixed = _mixed_name_pairs(top_level_polys)
                if mixed:
                    self.stdout.write('')
                    self.stdout.write(self.style.WARNING('  ⚠ Detected mixed continent names in POLYGON_FILES_DIR:'))
                    for underscored, hyphenated in mixed:
                        self.stdout.write(
                            f"    - Found both '{underscored}.poly' and '{hyphenated}.poly'; "
                            f"pipeline will treat '{hyphenated}' as canonical."
                        )
                    self.stdout.write(
                        '    Consider removing the underscore variants after verifying they are not needed.'
                    )
        else:
            self.stdout.write(
                self.style.WARNING('  ⚠ POLYGON_FILES_DIR is not configured; skipping polygon download.')
            )

        # Country pipeline profile sync (non-destructive, idempotent)
        try:
            self.stdout.write('')
            self.stdout.write('  Running country pipeline profile sync...')
            call_command('sync_country_pipeline_profiles')
        except Exception as e:
            raise RuntimeError(f'CountryPipelineProfile sync failed: {e}')
