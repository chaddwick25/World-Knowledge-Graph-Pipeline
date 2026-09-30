"""Seed the DataSource registry with known Canadian sources.

Phase 1 seeds Toronto Open Data (CKAN) and Statistics Canada (CKAN).
Additional sources (Vancouver, Ottawa, Montreal, Edmonton, Calgary,
GeoGratis, Mobility Database) can be added via
``register_geodata_source`` or extended here later.

The ``target_datasets`` list is inherited from the legacy
``toronto_data`` ``CkanMetadataService.TARGET_DATASETS``. Per-dataset
date fields mirror the legacy ``DatasetDownloadService.DATE_FIELD_MAPPING``
for incremental ingestion.
"""

from django.db import migrations

TORONTO_SOURCE = {
    'name': 'Toronto Open Data',
    'adapter_type': 'ckan',
    'base_url': 'https://ckan0.cf.opendata.inter.prod-toronto.ca',
    'country_code': 'CA',
    'config': {
        'api_version': '3',
        'ssl_verify': False,
        'timeout': 30,
        'user_agent': 'WorldKG-Geodata/1.0',
        'qa_dataset_id': 'catalogue-quality-scores',
        'request_delay_seconds': 0.5,
        'target_datasets': [
            'traffic-volumes-at-intersections-for-all-modes',
            'ttc-subway-delay-data',
            'cafeto-curb-lane-parklet-cafe-locations',
            'toronto-centreline-tcl',
            'intersection-file-city-of-toronto',
            'cycling-network',
            'neighbourhoods',
            'zoning-by-law',
            'business-improvement-areas',
            'permanent-bicycle-counters',
            'ttc-routes-and-schedules',
            'rain-gauge-locations-and-precipitation',
            'preliminary-zoning-reviews',
            'neighbourhood-profiles',
            'forest-and-land-cover',
            'committee-of-adjustment-applications',
        ],
        'dataset_date_fields': {
            'traffic-volumes-at-intersections-for-all-modes': 'count_date',
            'ttc-subway-delay-data': 'Date',
            'rain-gauge-locations-and-precipitation': 'date',
            'permanent-bicycle-counters': 'count_date',
            'preliminary-zoning-reviews': 'application_date',
            'committee-of-adjustment-applications': 'application_date',
        },
    },
}

STATCAN_SOURCE = {
    'name': 'Statistics Canada',
    'adapter_type': 'ckan',
    'base_url': 'https://open.canada.ca/data',
    'country_code': 'CA',
    'config': {
        'api_version': '3',
        'ssl_verify': True,
        'timeout': 30,
        'user_agent': 'WorldKG-Geodata/1.0',
        'request_delay_seconds': 0.5,
        'target_datasets': [],
    },
}


def seed_sources(apps, schema_editor):
    DataSource = apps.get_model('geodata', 'DataSource')
    for source_data in (TORONTO_SOURCE, STATCAN_SOURCE):
        DataSource.objects.update_or_create(
            name=source_data['name'],
            defaults=source_data,
        )


def unseed_sources(apps, schema_editor):
    DataSource = apps.get_model('geodata', 'DataSource')
    DataSource.objects.filter(name__in=[
        TORONTO_SOURCE['name'],
        STATCAN_SOURCE['name'],
    ]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('geodata', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_sources, unseed_sources),
    ]
