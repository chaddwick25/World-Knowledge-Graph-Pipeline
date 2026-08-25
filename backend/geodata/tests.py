"""Unit tests for the geodata app (adapter + ingestion + commands).

These tests mock the CKAN network layer (``fetch_dataset_metadata``,
``download_resource``) so they run without external access. The generic
record store is exercised with real CSV/GeoJSON parsing.
"""

import json

import pytest
from django.contrib.gis.geos import GEOSGeometry, Point
from django.core.management import call_command

from .models import (
    DataQualitySnapshot,
    DataSource,
    GeoDataset,
    GeoRecord,
    GeoResource,
)
from .services import IngestionService, QualityService

SAMPLE_PACKAGE = {
    'id': '11111111-2222-3333-4444-555555555555',
    'name': 'ttc-subway-delay-data',
    'title': 'TTC Subway Delay Data',
    'notes': 'Delay incidents',
    'refresh_rate': 'Daily',
    'organization': {'title': 'Toronto Transit Commission'},
    'license_title': 'Open Government Licence – Toronto',
    'resources': [
        {
            'id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'name': 'delay_data.csv',
            'format': 'CSV',
            'url': 'https://example.invalid/delay_data.csv',
            'size': 12345,
            'mimetype': 'text/csv',
            'last_modified': '2026-01-15T10:00:00Z',
        },
    ],
}


@pytest.fixture
def ckan_source(db):
    return DataSource.objects.create(
        name='Toronto Open Data',
        adapter_type='ckan',
        base_url='https://ckan0.cf.opendata.inter.prod-toronto.ca',
        config={
            'target_datasets': ['ttc-subway-delay-data'],
            'dataset_date_fields': {},
            'ssl_verify': False,
            'timeout': 5,
        },
    )


@pytest.fixture
def dataset_with_resource(ckan_source):
    dataset = GeoDataset.objects.create(
        source=ckan_source,
        source_dataset_id=SAMPLE_PACKAGE['id'],
        name=SAMPLE_PACKAGE['name'],
        title=SAMPLE_PACKAGE['title'],
    )
    resource = GeoResource.objects.create(
        dataset=dataset,
        source_resource_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        name='delay_data.csv',
        format='CSV',
        url='https://example.invalid/delay_data.csv',
    )
    return dataset, resource


# ---------------------------------------------------------------------------
# Metadata sync
# ---------------------------------------------------------------------------

def test_sync_metadata_creates_dataset_and_resources(ckan_source, monkeypatch):
    service = IngestionService(ckan_source)
    monkeypatch.setattr(service.adapter, 'fetch_dataset_metadata', lambda dataset_id: SAMPLE_PACKAGE)

    synced = service.sync_metadata(['ttc-subway-delay-data'])

    assert synced == 1
    dataset = GeoDataset.objects.get(source=ckan_source, name='ttc-subway-delay-data')
    assert dataset.title == 'TTC Subway Delay Data'
    assert dataset.publisher == 'Toronto Transit Commission'
    assert dataset.metadata['id'] == SAMPLE_PACKAGE['id']
    assert dataset.resources.count() == 1
    resource = dataset.resources.get()
    assert resource.format == 'CSV'
    assert resource.size_bytes == 12345
    assert resource.last_modified is not None


def test_sync_metadata_empty_targets_returns_zero(ckan_source):
    ckan_source.config = {'target_datasets': []}
    ckan_source.save()
    service = IngestionService(ckan_source)
    assert service.sync_metadata() == 0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_csv_yields_records_with_geometry(tmp_path, ckan_source):
    csv_file = tmp_path / 'traffic.csv'
    csv_file.write_text(
        '_id,count_date,location_name,longitude,latitude,vehicle_count\n'
        '1,2026-01-15,King St, -79.3832,43.6532,100\n'
        '2,2026-01-16,Queen St, -79.3781,43.6510,80\n',
        encoding='utf-8',
    )

    service = IngestionService(ckan_source)
    records = list(service.adapter.parse_resource(csv_file, date_field='count_date'))

    assert len(records) == 2
    first = records[0]
    assert first['source_id'] == '1'
    assert first['attributes']['location_name'] == 'King St'
    assert first['ingestion_date'].isoformat() == '2026-01-15'
    assert isinstance(first['geom'], Point)
    assert first['geom'].x == pytest.approx(-79.3832)
    assert first['geom'].y == pytest.approx(43.6532)


def test_parse_csv_geometry_column(tmp_path, ckan_source):
    csv_file = tmp_path / 'intersections.csv'
    # The geometry JSON is quoted, as in real Toronto Open Data exports.
    csv_file.write_text(
        '_id,INTERSECTION_ID,geometry\n'
        '1,100,"{""type"":""Point"",""coordinates"":[-79.4,43.7]}"\n',
        encoding='utf-8',
    )

    service = IngestionService(ckan_source)
    records = list(service.adapter.parse_resource(csv_file))

    assert len(records) == 1
    assert records[0]['source_id'] == '1'
    assert isinstance(records[0]['geom'], Point)
    assert records[0]['geom'].x == pytest.approx(-79.4)


def test_parse_json_labeled_csv_falls_back(tmp_path, ckan_source):
    # Some CKAN resources are labelled GEOJSON but the datastore dump
    # endpoint returns CSV — the adapter must fall back to row parsing.
    mislabeled = tmp_path / 'neighbourhoods.geojson'
    mislabeled.write_text(
        '_id,AREA_ID,AREA_NAME,geometry\n'
        '1,2501286,Brookhaven,"{""type"": ""Polygon"", ""coordinates"": [[[0,0],[1,0],[1,1],[0,0]]]}"\n',
        encoding='utf-8',
    )

    service = IngestionService(ckan_source)
    records = list(service.adapter.parse_resource(mislabeled))

    assert len(records) == 1
    assert records[0]['source_id'] == '1'
    assert records[0]['attributes']['AREA_NAME'] == 'Brookhaven'
    assert isinstance(records[0]['geom'], GEOSGeometry)


def test_parse_geojson_yields_records_with_polygon(tmp_path, ckan_source):
    geojson_file = tmp_path / 'neighbourhoods.geojson'
    geojson_file.write_text(json.dumps({
        'type': 'FeatureCollection',
        'features': [
            {
                'type': 'Feature',
                'id': 'hood-1',
                'properties': {'AREA_ID': 2501286, 'AREA_NAME': 'Brookhaven-Amesbury'},
                'geometry': {
                    'type': 'Polygon',
                    'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                },
            },
        ],
    }), encoding='utf-8')

    service = IngestionService(ckan_source)
    records = list(service.adapter.parse_resource(geojson_file))

    assert len(records) == 1
    assert records[0]['attributes']['AREA_NAME'] == 'Brookhaven-Amesbury'
    assert isinstance(records[0]['geom'], GEOSGeometry)
    assert records[0]['geom'].geom_type == 'Polygon'


# ---------------------------------------------------------------------------
# Download + ingest
# ---------------------------------------------------------------------------

def test_download_and_ingest_creates_records(dataset_with_resource, ckan_source, tmp_path, monkeypatch):
    dataset, resource = dataset_with_resource
    csv_file = tmp_path / 'delay_data.csv'
    csv_file.write_text(
        '_id,Date,Station,Min Delay\n'
        '1,2026-01-01,Union,5\n'
        '2,2026-01-02,Bloor,7\n',
        encoding='utf-8',
    )

    service = IngestionService(ckan_source)
    monkeypatch.setattr(service.adapter, 'download_resource', lambda res, force=False: csv_file)

    result = service.download_and_ingest(dataset.name)

    assert result.downloaded == 1
    assert result.records_ingested == 2
    assert GeoRecord.objects.filter(resource=resource).count() == 2
    record = GeoRecord.objects.get(resource=resource, source_id='1')
    assert record.attributes['Station'] == 'Union'
    assert record.dataset == dataset
    assert record.ingestion_date.isoformat() == '2026-01-01'


def test_download_and_ingest_incremental_skips_old_records(dataset_with_resource, ckan_source, tmp_path, monkeypatch):
    dataset, resource = dataset_with_resource
    csv_file = tmp_path / 'delay_data.csv'
    csv_file.write_text(
        '_id,Date,Station,Min Delay\n'
        '1,2026-01-01,Union,5\n'
        '2,2026-01-02,Bloor,7\n',
        encoding='utf-8',
    )

    service = IngestionService(ckan_source)
    monkeypatch.setattr(service.adapter, 'download_resource', lambda res, force=False: csv_file)

    # First run: incremental — creates the ingestion state, ingests everything
    result = service.download_and_ingest(dataset.name, incremental=True)
    assert result.records_ingested == 2

    # Second run: incremental — watermark at 2026-01-02, all records skipped
    result = service.download_and_ingest(dataset.name, incremental=True)
    assert result.records_ingested == 0
    assert GeoRecord.objects.filter(resource=resource).count() == 2

    state = resource.ingestion_state
    assert state.last_ingested_date.isoformat() == '2026-01-02'
    assert state.total_records_ingested == 2
    assert state.last_ingestion_mode == 'incremental'


# ---------------------------------------------------------------------------
# Quality snapshots
# ---------------------------------------------------------------------------

def test_quality_snapshots_created_from_records(dataset_with_resource, ckan_source, monkeypatch):
    dataset, _ = dataset_with_resource
    service = IngestionService(ckan_source)
    quality_service = QualityService(ckan_source, service.adapter)
    monkeypatch.setattr(
        service.adapter,
        'fetch_quality_records',
        lambda dataset_ids: [{
            'dataset_id': 'ttc-subway-delay-data',
            'score': 85.0,
            'grade': 'Gold',
            'freshness': 0.9,
            'metadata_score': 0.8,
            'usability': 0.9,
            'completeness': 0.85,
            'accessibility': 0.95,
            'qa_recorded_at': '2026-01-01T00:00:00Z',
        }],
    )

    snapshots = quality_service.sync_quality_snapshots(['ttc-subway-delay-data'])

    assert len(snapshots) == 1
    snapshot = DataQualitySnapshot.objects.get(dataset=dataset)
    assert snapshot.grade == 'Gold'
    assert snapshot.quality_score_pct == pytest.approx(85.0)
    assert snapshot.freshness == pytest.approx(0.9)

    # Re-sync is idempotent per (dataset, qa_recorded_at)
    again = quality_service.sync_quality_snapshots(['ttc-subway-delay-data'])
    assert len(again) == 1
    assert DataQualitySnapshot.objects.filter(dataset=dataset).count() == 1


def test_quality_snapshots_skip_unknown_datasets(ckan_source, monkeypatch):
    service = IngestionService(ckan_source)
    quality_service = QualityService(ckan_source, service.adapter)
    monkeypatch.setattr(
        service.adapter,
        'fetch_quality_records',
        lambda dataset_ids: [{'dataset_id': 'does-not-exist', 'grade': 'Gold'}],
    )

    assert quality_service.sync_quality_snapshots(['does-not-exist']) == []


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------

def test_register_geodata_source_command(db):
    call_command(
        'register_geodata_source',
        '--name', 'Vancouver Open Data',
        '--adapter', 'ckan',
        '--base-url', 'https://opendata.vancouver.ca',
        '--country-code', 'CA',
        '--config', '{"target_datasets": []}',
    )
    source = DataSource.objects.get(name='Vancouver Open Data')
    assert source.adapter_type == 'ckan'
    assert source.country_code == 'CA'


def test_list_geodata_sources_command(ckan_source, capsys):
    call_command('list_geodata_sources')
    out = capsys.readouterr().out
    assert 'Toronto Open Data' in out
