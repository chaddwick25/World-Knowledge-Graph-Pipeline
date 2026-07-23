import csv
import json
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, date, time
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from .base_ckan_service import BaseCkanService

# Increase CSV field size limit for large geometry fields
csv.field_size_limit(10485760)  # 10MB
from ..models import (
    CkanResource, TrafficVolume, TtcSubwayDelay, CafetoLocation,
    DatasetIngestionState,
    # Spatial Infrastructure
    TorontoCentreline, IntersectionFile, CyclingNetwork,
    Neighbourhood, ZoningByLaw, BusinessImprovementArea,
    # Temporal Flows
    BicycleCounter, TtcRoute, RainGauge, ZoningReview,
    NeighbourhoodProfile,
    # Additional
    ForestLandCover, CommitteeAdjustmentApplication
)


class DatasetDownloadService(BaseCkanService):
    """Service for downloading and parsing dataset files into database tables"""
    
    # Date field mapping for temporal datasets (for incremental updates)
    DATE_FIELD_MAPPING = {
        'traffic-volumes': 'count_date',
        'ttc-subway-delay': 'Date',
        'rain-gauge': 'date',
        'bicycle-counter': 'count_date',
        'zoning-review': 'application_date',
        'committee-adjustment': 'application_date',
    }
    
    # Dataset types: temporal (append-only) vs snapshot (replace)
    TEMPORAL_DATASETS = {
        'traffic-volumes', 'ttc-subway-delay', 'rain-gauge', 
        'bicycle-counter', 'zoning-review', 'committee-adjustment'
    }
    
    def __init__(self):
        super().__init__()
        self.download_dir = Path(getattr(settings, 'TORONTO_DATA_DIR', '/tmp/toronto_data_downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.bulk_batch_size = getattr(settings, 'TORONTO_BULK_INSERT_BATCH_SIZE', 1000)
    
    def _should_skip_record(self, record_date: date, ingestion_state: Optional['DatasetIngestionState']) -> bool:
        """
        Check if a record should be skipped based on incremental ingestion state
        
        Args:
            record_date: Date of the record
            ingestion_state: Ingestion state object (None if not incremental mode)
            
        Returns:
            True if record should be skipped (already ingested), False otherwise
        """
        if not ingestion_state or not record_date:
            return False
        
        last_date = ingestion_state.last_ingested_date
        if not last_date:
            return False
        
        # Skip records on or before last ingested date
        return record_date <= last_date
    
    def _update_ingestion_state(self, ingestion_state: Optional['DatasetIngestionState'], 
                                latest_date: Optional[date], records_count: int, mode: str = 'incremental'):
        """
        Update ingestion state after successful import
        
        Args:
            ingestion_state: Ingestion state object
            latest_date: Latest date in the imported batch
            records_count: Number of records imported
            mode: 'full' or 'incremental'
        """
        if not ingestion_state:
            return
        
        if latest_date:
            if not ingestion_state.last_ingested_date or latest_date > ingestion_state.last_ingested_date:
                ingestion_state.last_ingested_date = latest_date
        
        ingestion_state.last_ingested_count = records_count
        ingestion_state.total_records_ingested += records_count
        ingestion_state.last_ingestion_mode = mode
        ingestion_state.save()
        
        self.logger.info(f"Updated ingestion state: latest_date={latest_date}, count={records_count}, total={ingestion_state.total_records_ingested}")
    
    def download_resource(self, resource: CkanResource, force: bool = False) -> Optional[Path]:
        """
        Download a resource file to local storage
        
        Args:
            resource: CkanResource instance
            force: Force download even if already downloaded
            
        Returns:
            Path to downloaded file or None if failed
        """
        # Check if we need to download
        if not force and resource.last_downloaded_at:
            if resource.last_modified and resource.last_downloaded_at >= resource.last_modified:
                self.logger.info(f"Resource {resource.name} is up to date, skipping download")
                return None
        
        # Download file
        filename = f"{resource.ckan_resource_id}.{resource.format.lower()}"
        file_path = self.download_dir / filename
        
        try:
            self.logger.info(f"Downloading {resource.name} from {resource.url}")
            req = urllib.request.Request(resource.url, headers={"User-Agent": self.user_agent})
            response = urllib.request.urlopen(
                req,
                context=self._get_ssl_context(),
                timeout=self.timeout
            )
            
            with open(file_path, 'wb') as f:
                f.write(response.read())
            
            # Update download timestamp
            resource.last_downloaded_at = timezone.now()
            resource.save(update_fields=['last_downloaded_at'])
            
            self.logger.info(f"Downloaded {resource.name} to {file_path}")
            return file_path
            
        except Exception as e:
            self.logger.error(f"Failed to download {resource.name}: {e}")
            return None
    
    def parse_and_import(self, resource: CkanResource, file_path: Path, truncate: bool = True, incremental: bool = False):
        """
        Parse file and import into appropriate content table
        
        Args:
            resource: CkanResource instance
            file_path: Path to downloaded file
            truncate: If True, delete existing data before import
            incremental: If True, only import records newer than last ingestion
        """
        dataset = resource.dataset
        dataset_title = dataset.title.lower()
        dataset_name = dataset.name.lower()
        
        # Get or create ingestion state for incremental mode
        ingestion_state = None
        if incremental:
            ingestion_state, _ = DatasetIngestionState.objects.get_or_create(resource=resource)
            self.logger.info(f"Incremental mode: last ingested date = {ingestion_state.last_ingested_date or ingestion_state.last_ingested_datetime or 'Never'}")
        
        # Route to appropriate parser based on dataset name (most reliable)
        # Spatial Infrastructure
        if 'centreline' in dataset_name:
            self._import_centreline_csv(resource, file_path, truncate, ingestion_state)
        elif 'intersection' in dataset_name:
            self._import_intersection_csv(resource, file_path, truncate, ingestion_state)
        elif 'cycling' in dataset_name:
            self._import_cycling_csv(resource, file_path, truncate, ingestion_state)
        elif 'neighbourhood' in dataset_name and 'profile' not in dataset_name:
            self._import_neighbourhood_csv(resource, file_path, truncate, ingestion_state)
        elif 'zoning-by-law' in dataset_name or 'zoning_by_law' in dataset_name:
            self._import_zoning_bylaw_csv(resource, file_path, truncate, ingestion_state)
        elif 'business-improvement' in dataset_name or 'bia' in dataset_name:
            self._import_bia_csv(resource, file_path, truncate, ingestion_state)
        
        # Temporal Flows
        elif 'traffic' in dataset_title or 'traffic' in dataset_name:
            self._import_traffic_csv(resource, file_path, truncate, ingestion_state)
        elif 'bicycle-counter' in dataset_name or 'bike-counter' in dataset_name:
            self._import_bicycle_counter_csv(resource, file_path, truncate, ingestion_state)
        elif 'ttc' in dataset_name and ('route' in dataset_name or 'schedule' in dataset_name):
            self._import_ttc_route_csv(resource, file_path, truncate, ingestion_state)
        elif 'ttc' in dataset_title or 'subway' in dataset_title or 'delay' in dataset_title:
            self._import_ttc_csv(resource, file_path, truncate, ingestion_state)
        elif 'rain' in dataset_name or 'precipitation' in dataset_name:
            self._import_rain_gauge_csv(resource, file_path, truncate, ingestion_state)
        elif 'zoning-review' in dataset_name or 'preliminary-zoning' in dataset_name:
            self._import_zoning_review_csv(resource, file_path, truncate, ingestion_state)
        elif 'neighbourhood-profile' in dataset_name or 'census' in dataset_name:
            self._import_neighbourhood_profile_csv(resource, file_path, truncate, ingestion_state)
        
        # Additional
        elif 'caf' in dataset_title or 'parklet' in dataset_title or 'curb' in dataset_title:
            self._import_cafeto_csv(resource, file_path, truncate)
        elif 'forest' in dataset_name or 'land-cover' in dataset_name:
            self._import_forest_landcover_csv(resource, file_path, truncate)
        elif 'committee' in dataset_name and 'adjustment' in dataset_name:
            self._import_committee_adjustment_csv(resource, file_path, truncate)
        
        else:
            self.logger.warning(f"Unknown dataset type: {dataset.title} (ID: {dataset.ckan_id})")
    
    def _import_traffic_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import traffic volume CSV"""
        self.logger.info(f"Importing traffic data from {file_path}")
        
        if truncate:
            deleted_count = TrafficVolume.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing traffic records")
        
        rows = []
        skipped = 0
        latest_date = None
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Use count_date or latest_count_date
                date_str = row_data.get('count_date') or row_data.get('latest_count_date')
                record_date = self._parse_date(date_str)
                
                # Skip if incremental mode and record is already ingested
                if self._should_skip_record(record_date, ingestion_state):
                    skipped += 1
                    continue
                
                # Track latest date for ingestion state update
                if record_date and (not latest_date or record_date > latest_date):
                    latest_date = record_date
                
                # Calculate total vehicle count from available columns
                vehicle_count = None
                if 'total_vehicle' in row_data:
                    vehicle_count = self._parse_int(row_data.get('total_vehicle'))
                
                # Get pedestrian and bike counts
                pedestrian_count = self._parse_int(row_data.get('total_pedestrian'))
                cyclist_count = self._parse_int(row_data.get('total_bike'))
                
                traffic_row = TrafficVolume(
                    resource=resource,
                    intersection_id=row_data.get('count_id', row_data.get('latest_count_id', '')),
                    location=row_data.get('location_name', ''),
                    date=record_date,
                    time_period=row_data.get('start_time', ''),
                    vehicle_count=vehicle_count,
                    pedestrian_count=pedestrian_count,
                    cyclist_count=cyclist_count,
                    latitude=self._parse_float(row_data.get('latitude')),
                    longitude=self._parse_float(row_data.get('longitude')),
                    raw_data=row_data,
                )
                rows.append(traffic_row)
                
                # Bulk insert in batches
                if len(rows) >= self.bulk_batch_size:
                    TrafficVolume.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} traffic records")
                    rows = []
        
        # Insert remaining rows
        if rows:
            TrafficVolume.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} traffic records")
        
        if skipped > 0:
            self.logger.info(f"Skipped {skipped} records (already ingested)")
        
        # Update ingestion state
        total_imported = TrafficVolume.objects.filter(resource=resource).count()
        mode = 'full' if truncate else 'incremental'
        self._update_ingestion_state(ingestion_state, latest_date, len(rows), mode)
        
        self.logger.info(f"Total traffic records for resource: {total_imported}")
    
    def _import_ttc_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import TTC delay CSV"""
        # Skip Code Descriptions files - they're reference data, not delay records
        if 'code' in resource.name.lower() and 'description' in resource.name.lower():
            self.logger.info(f"Skipping Code Descriptions file: {resource.name}")
            return
        
        self.logger.info(f"Importing TTC delay data from {file_path}")
        
        if truncate:
            deleted_count = TtcSubwayDelay.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing TTC delay records")
        
        rows = []
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Actual column names from Toronto Open Data:
                # Date, Time, Station, Code, Min Delay, Bound, Line, Vehicle
                # Skip rows without required fields
                if not row_data.get('Date') or not row_data.get('Station'):
                    continue
                
                ttc_row = TtcSubwayDelay(
                    resource=resource,
                    date=self._parse_date(row_data.get('Date')),
                    time=self._parse_time(row_data.get('Time')),
                    station=row_data.get('Station', ''),
                    line=row_data.get('Line', ''),
                    delay_minutes=self._parse_int(row_data.get('Min Delay')),
                    delay_code=row_data.get('Code', ''),
                    delay_reason=row_data.get('Bound', ''),  # Bound = direction (E/W/N/S)
                    vehicle_number=str(row_data.get('Vehicle', '')),
                    raw_data=row_data,
                )
                rows.append(ttc_row)
                
                if len(rows) >= self.bulk_batch_size:
                    TtcSubwayDelay.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} TTC delay records")
                    rows = []
        
        if rows:
            TtcSubwayDelay.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} TTC delay records")
        
        total = TtcSubwayDelay.objects.filter(resource=resource).count()
        self.logger.info(f"Total TTC delay records for resource: {total}")
    
    def _import_cafeto_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import CaféTO location CSV"""
        self.logger.info(f"Importing CaféTO data from {file_path}")
        
        if truncate:
            deleted_count = CafetoLocation.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing CaféTO records")
        
        rows = []
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Actual Toronto Open Data columns:
                # OPERATOR_NAME, BUSINESS_ADDRESS, WARD_NAME, geometry (JSON with coordinates)
                
                # Extract coordinates from geometry JSON
                lat, lon = None, None
                geometry_str = row_data.get('geometry', '')
                if geometry_str:
                    try:
                        import json
                        geometry = json.loads(geometry_str)
                        if 'coordinates' in geometry and geometry['coordinates']:
                            # Coordinates are in [x, y] format (lon, lat or projected coords)
                            coords = geometry['coordinates'][0]
                            if len(coords) >= 2:
                                # These appear to be projected coordinates, not lat/lon
                                # Store them as-is for now
                                lon = coords[0]
                                lat = coords[1]
                    except Exception as e:
                        self.logger.warning(f"Failed to parse geometry: {e}")
                
                cafeto_row = CafetoLocation(
                    resource=resource,
                    business_name=row_data.get('OPERATOR_NAME', ''),
                    address=row_data.get('BUSINESS_ADDRESS', row_data.get('MUNICIPAL_ADDRESS', '')),
                    ward=row_data.get('WARD_NAME', ''),
                    latitude=lat or 0.0,  # Required field, use 0 if not available
                    longitude=lon or 0.0,  # Required field, use 0 if not available
                    installation_date=None,  # Not in this dataset
                    status=row_data.get('CATEGORY', row_data.get('INTERVENTION_TYPE', '')),
                    raw_data=row_data,
                )
                rows.append(cafeto_row)
                
                if len(rows) >= self.bulk_batch_size:
                    CafetoLocation.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} CaféTO records")
                    rows = []
        
        if rows:
            CafetoLocation.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} CaféTO records")
        
        total = CafetoLocation.objects.filter(resource=resource).count()
        self.logger.info(f"Total CaféTO records for resource: {total}")
    
    def _parse_date(self, date_str: str) -> Optional[date]:
        """Parse date string"""
        if not date_str:
            return None
        try:
            return parse_date(date_str)
        except Exception:
            return None
    
    def _parse_time(self, time_str: str) -> Optional[time]:
        """Parse time string"""
        if not time_str:
            return None
        try:
            return parse_time(time_str)
        except Exception:
            return None
    
    def _parse_int(self, value: str) -> Optional[int]:
        """Parse integer value"""
        if not value:
            return None
        try:
            return int(float(value))
        except Exception:
            return None
    
    def _parse_float(self, value: str) -> Optional[float]:
        """Parse float value"""
        if not value:
            return None
        try:
            return float(value)
        except Exception:
            return None
    
    # ========================================================================
    # SPATIAL INFRASTRUCTURE PARSERS
    # ========================================================================
    
    def _import_centreline_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Toronto Centreline CSV/GeoJSON"""
        self.logger.info(f"Importing centreline data from {file_path}")
        
        if truncate:
            deleted_count = TorontoCentreline.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing centreline records")
        
        rows = []
        # Handle both CSV and GeoJSON formats
        if file_path.suffix.lower() == '.geojson' or file_path.suffix.lower() == '.json':
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = TorontoCentreline(
                        resource=resource,
                        centreline_id=str(props.get('CENTRELINE_ID', props.get('GEO_ID', ''))),
                        linear_name_full=props.get('LINEAR_NAME_FULL', props.get('LNAME', '')),
                        address_l=props.get('ADDRESS_L', ''),
                        address_r=props.get('ADDRESS_R', ''),
                        from_intersection_id=str(props.get('FROM_INTERSECTION_ID', '')),
                        to_intersection_id=str(props.get('TO_INTERSECTION_ID', '')),
                        feature_code=str(props.get('FEATURE_CODE', props.get('FCODE', ''))),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        TorontoCentreline.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} centreline records")
                        rows = []
        
        if rows:
            TorontoCentreline.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} centreline records")
        
        total = TorontoCentreline.objects.filter(resource=resource).count()
        self.logger.info(f"Total centreline records: {total}")
    
    def _import_intersection_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Intersection File CSV/GeoJSON"""
        self.logger.info(f"Importing intersection data from {file_path}")
        
        if truncate:
            deleted_count = IntersectionFile.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing intersection records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    coords = geometry.get('coordinates', [])
                    
                    row = IntersectionFile(
                        resource=resource,
                        intersection_id=str(props.get('INTERSECTION_ID', props.get('INT_ID', ''))),
                        intersection_desc=props.get('INTERSECTION_DESC', props.get('DESCRIPTION', '')),
                        latitude=coords[1] if len(coords) >= 2 else 0.0,
                        longitude=coords[0] if len(coords) >= 2 else 0.0,
                        elevation=self._parse_float(props.get('ELEVATION')),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        IntersectionFile.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} intersection records")
                        rows = []
        
        if rows:
            IntersectionFile.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} intersection records")
        
        total = IntersectionFile.objects.filter(resource=resource).count()
        self.logger.info(f"Total intersection records: {total}")
    
    def _import_cycling_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Cycling Network CSV/GeoJSON"""
        self.logger.info(f"Importing cycling network data from {file_path}")
        
        if truncate:
            deleted_count = CyclingNetwork.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing cycling network records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = CyclingNetwork(
                        resource=resource,
                        segment_id=str(props.get('SEGMENT_ID', props.get('ID', ''))),
                        street_name=props.get('STREET_NAME', props.get('NAME', '')),
                        infrastructure_type=props.get('INFRA_TYPE', props.get('TYPE', '')),
                        from_street=props.get('FROM_STREET', ''),
                        to_street=props.get('TO_STREET', ''),
                        length_m=self._parse_float(props.get('LENGTH_M', props.get('LENGTH', ''))),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        CyclingNetwork.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} cycling network records")
                        rows = []
        
        if rows:
            CyclingNetwork.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} cycling network records")
        
        total = CyclingNetwork.objects.filter(resource=resource).count()
        self.logger.info(f"Total cycling network records: {total}")
    
    def _import_neighbourhood_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Neighbourhood boundaries CSV/GeoJSON"""
        self.logger.info(f"Importing neighbourhood data from {file_path}")
        
        if truncate:
            deleted_count = Neighbourhood.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing neighbourhood records")
        
        rows = []
        
        # Try to detect if it's actually CSV by reading first line
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            first_line = f.readline().strip()
        
        # If first line has commas and looks like CSV header, treat as CSV
        if ',' in first_line and ('AREA_ID' in first_line or 'HOOD_ID' in first_line):
            self.logger.info("Detected CSV format")
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                
                for row_data in reader:
                    # Parse geometry from WKT or JSON string if present
                    geometry_data = {}
                    if 'geometry' in row_data and row_data['geometry']:
                        try:
                            geometry_data = json.loads(row_data['geometry'])
                        except:
                            geometry_data = {'type': 'Polygon', 'coordinates': []}
                    
                    row = Neighbourhood(
                        resource=resource,
                        neighbourhood_id=str(row_data.get('AREA_ID', row_data.get('HOOD_ID', ''))),
                        neighbourhood_name=row_data.get('AREA_NAME', row_data.get('HOOD_NAME', '')),
                        area_sqkm=self._parse_float(row_data.get('AREA_SQKM', row_data.get('AREA', ''))),
                        geometry=geometry_data,
                        raw_data=row_data,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        Neighbourhood.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} neighbourhood records")
                        rows = []
        
        elif file_path.suffix.lower() in ['.geojson', '.json']:
            # Try GeoJSON format
            try:
                with open(file_path, 'r', encoding='utf-8-sig') as f:
                    content = f.read().strip()
                    if not content:
                        self.logger.warning(f"Empty file: {file_path}")
                        return
                    data = json.loads(content)
                
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = Neighbourhood(
                        resource=resource,
                        neighbourhood_id=str(props.get('AREA_ID', props.get('HOOD_ID', ''))),
                        neighbourhood_name=props.get('AREA_NAME', props.get('HOOD_NAME', '')),
                        area_sqkm=self._parse_float(props.get('AREA_SQKM', props.get('AREA', ''))),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        Neighbourhood.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} neighbourhood records")
                        rows = []
            except json.JSONDecodeError as e:
                self.logger.error(f"Invalid JSON in {file_path}: {e}")
                return
            except Exception as e:
                self.logger.error(f"Error reading {file_path}: {e}")
                return
        
        if rows:
            Neighbourhood.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} neighbourhood records")
        
        total = Neighbourhood.objects.filter(resource=resource).count()
        self.logger.info(f"Total neighbourhood records: {total}")
    
    def _import_zoning_bylaw_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Zoning By-Law GeoJSON"""
        self.logger.info(f"Importing zoning by-law data from {file_path}")
        
        if truncate:
            deleted_count = ZoningByLaw.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing zoning records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = ZoningByLaw(
                        resource=resource,
                        zone_id=str(props.get('ZONE_ID', props.get('ID', ''))),
                        zone_category=props.get('ZONE_CATEGORY', props.get('CATEGORY', '')),
                        zone_label=props.get('ZONE_LABEL', props.get('LABEL', '')),
                        description=props.get('DESCRIPTION', ''),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        ZoningByLaw.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} zoning records")
                        rows = []
        
        if rows:
            ZoningByLaw.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} zoning records")
        
        total = ZoningByLaw.objects.filter(resource=resource).count()
        self.logger.info(f"Total zoning records: {total}")
    
    def _import_bia_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Business Improvement Areas GeoJSON"""
        self.logger.info(f"Importing BIA data from {file_path}")
        
        if truncate:
            deleted_count = BusinessImprovementArea.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing BIA records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = BusinessImprovementArea(
                        resource=resource,
                        bia_id=str(props.get('BIA_ID', props.get('ID', ''))),
                        bia_name=props.get('BIA_NAME', props.get('NAME', '')),
                        area_sqkm=self._parse_float(props.get('AREA_SQKM', props.get('AREA', ''))),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        BusinessImprovementArea.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} BIA records")
                        rows = []
        
        if rows:
            BusinessImprovementArea.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} BIA records")
        
        total = BusinessImprovementArea.objects.filter(resource=resource).count()
        self.logger.info(f"Total BIA records: {total}")
    
    # ========================================================================
    # TEMPORAL FLOW PARSERS
    # ========================================================================
    
    def _import_bicycle_counter_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Bicycle Counter CSV"""
        self.logger.info(f"Importing bicycle counter data from {file_path}")
        
        if truncate:
            deleted_count = BicycleCounter.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing bicycle counter records")
        
        rows = []
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                row = BicycleCounter(
                    resource=resource,
                    location_id=row_data.get('LOCATION_ID', row_data.get('ID', '')),
                    location_name=row_data.get('LOCATION_NAME', row_data.get('NAME', '')),
                    count_date=self._parse_date(row_data.get('COUNT_DATE', row_data.get('DATE', ''))),
                    count_time=self._parse_time(row_data.get('COUNT_TIME', row_data.get('TIME', ''))),
                    count_value=self._parse_int(row_data.get('COUNT', row_data.get('VALUE', '0'))),
                    latitude=self._parse_float(row_data.get('LATITUDE', row_data.get('LAT', ''))),
                    longitude=self._parse_float(row_data.get('LONGITUDE', row_data.get('LON', ''))),
                    raw_data=row_data,
                )
                rows.append(row)
                
                if len(rows) >= self.bulk_batch_size:
                    BicycleCounter.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} bicycle counter records")
                    rows = []
        
        if rows:
            BicycleCounter.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} bicycle counter records")
        
        total = BicycleCounter.objects.filter(resource=resource).count()
        self.logger.info(f"Total bicycle counter records: {total}")
    
    def _import_ttc_route_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import TTC Routes CSV/GeoJSON"""
        self.logger.info(f"Importing TTC route data from {file_path}")
        
        if truncate:
            deleted_count = TtcRoute.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing TTC route records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = TtcRoute(
                        resource=resource,
                        route_id=str(props.get('ROUTE_ID', props.get('ID', ''))),
                        route_name=props.get('ROUTE_NAME', props.get('NAME', '')),
                        route_type=props.get('ROUTE_TYPE', props.get('TYPE', '')),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        TtcRoute.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} TTC route records")
                        rows = []
        
        if rows:
            TtcRoute.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} TTC route records")
        
        total = TtcRoute.objects.filter(resource=resource).count()
        self.logger.info(f"Total TTC route records: {total}")
    
    def _import_rain_gauge_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Rain Gauge CSV"""
        self.logger.info(f"Importing rain gauge data from {file_path}")
        
        if truncate:
            deleted_count = RainGauge.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing rain gauge records")
        
        rows = []
        skipped = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Parse date - handle both date and datetime formats
                date_str = row_data.get('date', row_data.get('DATE', row_data.get('MEASUREMENT_DATE', '')))
                if date_str and 'T' in date_str:
                    # ISO datetime format - extract date part
                    date_str = date_str.split('T')[0]
                
                measurement_date = self._parse_date(date_str)
                
                # Skip rows without required date
                if not measurement_date:
                    skipped += 1
                    continue
                
                row = RainGauge(
                    resource=resource,
                    station_id=row_data.get('name', row_data.get('STATION_ID', row_data.get('ID', ''))),
                    station_name=row_data.get('name', row_data.get('STATION_NAME', row_data.get('NAME', ''))),
                    measurement_date=measurement_date,
                    measurement_time=self._parse_time(row_data.get('TIME', row_data.get('MEASUREMENT_TIME', ''))),
                    precipitation_mm=self._parse_float(row_data.get('precipitation', row_data.get('PRECIPITATION', row_data.get('PRECIP_MM', '0')))),
                    latitude=self._parse_float(row_data.get('latitude', row_data.get('LATITUDE', row_data.get('LAT', '')))),
                    longitude=self._parse_float(row_data.get('longitude', row_data.get('LONGITUDE', row_data.get('LON', '')))),
                    raw_data=row_data,
                )
                rows.append(row)
                
                if len(rows) >= self.bulk_batch_size:
                    RainGauge.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} rain gauge records")
                    rows = []
        
        if rows:
            RainGauge.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} rain gauge records")
        
        if skipped > 0:
            self.logger.warning(f"Skipped {skipped} rows with missing required fields")
        
        total = RainGauge.objects.filter(resource=resource).count()
        self.logger.info(f"Total rain gauge records: {total}")
    
    def _import_zoning_review_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Zoning Review CSV"""
        self.logger.info(f"Importing zoning review data from {file_path}")
        
        if truncate:
            deleted_count = ZoningReview.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing zoning review records")
        
        rows = []
        skipped = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Get application ID - skip if empty (would violate unique constraint)
                app_id = row_data.get('APPLICATION_ID', row_data.get('ID', '')).strip()
                if not app_id:
                    skipped += 1
                    continue
                
                row = ZoningReview(
                    resource=resource,
                    application_id=app_id,
                    application_date=self._parse_date(row_data.get('APPLICATION_DATE', row_data.get('DATE', ''))),
                    address=row_data.get('ADDRESS', ''),
                    proposal_description=row_data.get('DESCRIPTION', row_data.get('PROPOSAL', '')),
                    status=row_data.get('STATUS', ''),
                    latitude=self._parse_float(row_data.get('LATITUDE', row_data.get('LAT', ''))),
                    longitude=self._parse_float(row_data.get('LONGITUDE', row_data.get('LON', ''))),
                    raw_data=row_data,
                )
                rows.append(row)
                
                if len(rows) >= self.bulk_batch_size:
                    ZoningReview.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} zoning review records")
                    rows = []
        
        if rows:
            ZoningReview.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} zoning review records")
        
        if skipped > 0:
            self.logger.warning(f"Skipped {skipped} rows with missing application ID")
        
        total = ZoningReview.objects.filter(resource=resource).count()
        self.logger.info(f"Total zoning review records: {total}")
    
    def _import_neighbourhood_profile_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Neighbourhood Profile CSV"""
        self.logger.info(f"Importing neighbourhood profile data from {file_path}")
        
        if truncate:
            deleted_count = NeighbourhoodProfile.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing neighbourhood profile records")
        
        rows = []
        skipped = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Get neighbourhood ID and census year - skip if empty (unique constraint)
                hood_id = row_data.get('NEIGHBOURHOOD_ID', row_data.get('HOOD_ID', '')).strip()
                census_year = self._parse_int(row_data.get('CENSUS_YEAR', row_data.get('YEAR', '0')))
                
                if not hood_id or not census_year:
                    skipped += 1
                    continue
                
                row = NeighbourhoodProfile(
                    resource=resource,
                    neighbourhood_id=hood_id,
                    census_year=census_year,
                    population=self._parse_int(row_data.get('POPULATION', '')),
                    households=self._parse_int(row_data.get('HOUSEHOLDS', '')),
                    median_income=self._parse_float(row_data.get('MEDIAN_INCOME', '')),
                    raw_data=row_data,
                )
                rows.append(row)
                
                if len(rows) >= self.bulk_batch_size:
                    NeighbourhoodProfile.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} neighbourhood profile records")
                    rows = []
        
        if rows:
            NeighbourhoodProfile.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} neighbourhood profile records")
        
        if skipped > 0:
            self.logger.warning(f"Skipped {skipped} rows with missing neighbourhood ID or census year")
        
        total = NeighbourhoodProfile.objects.filter(resource=resource).count()
        self.logger.info(f"Total neighbourhood profile records: {total}")
    
    # ========================================================================
    # ADDITIONAL PARSERS
    # ========================================================================
    
    def _import_forest_landcover_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Forest Land Cover GeoJSON"""
        self.logger.info(f"Importing forest land cover data from {file_path}")
        
        if truncate:
            deleted_count = ForestLandCover.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing forest land cover records")
        
        rows = []
        if file_path.suffix.lower() in ['.geojson', '.json']:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                
                for feature in features:
                    props = feature.get('properties', {})
                    geometry = feature.get('geometry', {})
                    
                    row = ForestLandCover(
                        resource=resource,
                        feature_id=str(props.get('FEATURE_ID', props.get('ID', ''))),
                        land_cover_type=props.get('LAND_COVER_TYPE', props.get('TYPE', '')),
                        area_sqm=self._parse_float(props.get('AREA_SQM', props.get('AREA', ''))),
                        geometry=geometry,
                        raw_data=props,
                    )
                    rows.append(row)
                    
                    if len(rows) >= self.bulk_batch_size:
                        ForestLandCover.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                        self.logger.info(f"Inserted {len(rows)} forest land cover records")
                        rows = []
        
        if rows:
            ForestLandCover.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} forest land cover records")
        
        total = ForestLandCover.objects.filter(resource=resource).count()
        self.logger.info(f"Total forest land cover records: {total}")
    
    def _import_committee_adjustment_csv(self, resource: CkanResource, file_path: Path, truncate: bool, ingestion_state: Optional['DatasetIngestionState'] = None):
        """Parse and import Committee Adjustment Applications CSV"""
        self.logger.info(f"Importing committee adjustment data from {file_path}")
        
        if truncate:
            deleted_count = CommitteeAdjustmentApplication.objects.filter(resource=resource).delete()[0]
            self.logger.info(f"Deleted {deleted_count} existing committee adjustment records")
        
        rows = []
        skipped = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_data in reader:
                # Get application number - skip if empty (unique constraint)
                app_num = row_data.get('APPLICATION_NUMBER', row_data.get('APP_NUM', '')).strip()
                app_date = self._parse_date(row_data.get('APPLICATION_DATE', row_data.get('DATE', '')))
                
                # Skip if missing required fields
                if not app_num or not app_date:
                    skipped += 1
                    continue
                
                row = CommitteeAdjustmentApplication(
                    resource=resource,
                    application_number=app_num,
                    application_date=app_date,
                    address=row_data.get('ADDRESS', ''),
                    application_type=row_data.get('APPLICATION_TYPE', row_data.get('TYPE', '')),
                    status=row_data.get('STATUS', ''),
                    latitude=self._parse_float(row_data.get('LATITUDE', row_data.get('LAT', ''))),
                    longitude=self._parse_float(row_data.get('LONGITUDE', row_data.get('LON', ''))),
                    raw_data=row_data,
                )
                rows.append(row)
                
                if len(rows) >= self.bulk_batch_size:
                    CommitteeAdjustmentApplication.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
                    self.logger.info(f"Inserted {len(rows)} committee adjustment records")
                    rows = []
        
        if rows:
            CommitteeAdjustmentApplication.objects.bulk_create(rows, batch_size=self.bulk_batch_size)
            self.logger.info(f"Inserted {len(rows)} committee adjustment records")
        
        if skipped > 0:
            self.logger.warning(f"Skipped {skipped} rows with missing application number or date")
        
        total = CommitteeAdjustmentApplication.objects.filter(resource=resource).count()
        self.logger.info(f"Total committee adjustment records: {total}")
