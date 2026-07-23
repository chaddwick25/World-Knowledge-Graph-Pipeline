"""
Polygon to GeoJSON Conversion Service

Converts .poly files to GeoJSON format for use in Leaflet maps.
Caches results in PolygonFile model to avoid re-parsing.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Union
from django.contrib.gis.geos import Polygon as GeosPolygon
from django.contrib.gis.geos import MultiPolygon as GeosMultiPolygon
from extraction.models import PolygonFile
from extraction.utils.poly_parser import parse_poly_file

logger = logging.getLogger(__name__)


class PolygonGeoJsonService:
    """Service for converting polygon files to GeoJSON format"""
    
    @staticmethod
    def convert_poly_to_geojson(poly_path: str) -> Dict:
        """
        Convert a .poly file to GeoJSON format.
        
        Args:
            poly_path: Path to .poly file
            
        Returns:
            dict: GeoJSON representation of the polygon
            
        Raises:
            ValueError: If polygon file cannot be parsed
        """
        try:
            # Parse .poly file to GEOS Polygon
            geos_polygon = parse_poly_file(poly_path)
            
            # Convert GEOS Polygon to GeoJSON
            geojson = PolygonGeoJsonService._geos_to_geojson(geos_polygon)
            
            return geojson
            
        except Exception as e:
            logger.error(f"Failed to convert {poly_path} to GeoJSON: {str(e)}")
            raise ValueError(f"Cannot parse polygon file: {str(e)}")
    
    @staticmethod
    def _geos_to_geojson(
        geos_geom: Union[GeosPolygon, GeosMultiPolygon]
    ) -> Dict:
        """
        Convert Django GEOS Polygon or MultiPolygon to GeoJSON format.
        
        Args:
            geos_geom: Django GEOS Polygon or MultiPolygon object
            
        Returns:
            dict: GeoJSON representation (Polygon or MultiPolygon)
        """
        if isinstance(geos_geom, GeosMultiPolygon):
            # Handle MultiPolygon — convert each sub-polygon
            all_coords = []
            for sub_poly in geos_geom:
                raw_ring = sub_poly.coords[0]
                ring = [[round(lon, 5), round(lat, 5)] for lon, lat in raw_ring]
                ring = PolygonGeoJsonService._simplify_ring(ring)
                all_coords.append([ring])
            return {
                "type": "MultiPolygon",
                "coordinates": all_coords
            }
        else:
            # Handle single Polygon
            raw_coords = geos_geom.coords[0]
            coords = [[round(lon, 5), round(lat, 5)] for lon, lat in raw_coords]
            coords = PolygonGeoJsonService._simplify_ring(coords)
            return {
                "type": "Polygon",
                "coordinates": [coords]
            }

    @staticmethod
    def _simplify_ring(coords: list) -> list:
        """
        Simplify a coordinate ring to keep GeoJSON payload manageable.
        
        Args:
            coords: List of [lon, lat] pairs
            
        Returns:
            Simplified list of [lon, lat] pairs
        """
        if len(coords) > 500:
            step = max(1, len(coords) // 400)
            simplified = coords[::step]
            # Ensure the ring is still closed
            if simplified[0] != simplified[-1]:
                simplified.append(simplified[0])
            return simplified
        return coords
    
    @staticmethod
    def get_or_generate_geojson(polygon_file: PolygonFile) -> Dict:
        """
        Get cached GeoJSON or generate it if not cached.
        
        Args:
            polygon_file: PolygonFile model instance
            
        Returns:
            dict: GeoJSON representation
        """
        # Check if GeoJSON is already cached
        if polygon_file.geojson:
            return polygon_file.geojson
        
        # Generate GeoJSON
        try:
            geojson = PolygonGeoJsonService.convert_poly_to_geojson(
                polygon_file.file_path
            )
            
            # Cache in database
            polygon_file.geojson = geojson
            polygon_file.save(update_fields=['geojson', 'geojson_generated_at'])
            
            logger.info(f"Generated and cached GeoJSON for {polygon_file.name}")
            
            return geojson
            
        except Exception as e:
            logger.error(f"Failed to generate GeoJSON for {polygon_file.name}: {str(e)}")
            # Return a simple bounding box as fallback
            return PolygonGeoJsonService._generate_fallback_geojson(polygon_file)
    
    @staticmethod
    def _generate_fallback_geojson(polygon_file: PolygonFile) -> Dict:
        """
        Generate a simple bounding box GeoJSON as fallback.
        
        This is used when the actual polygon file cannot be parsed.
        Returns a very rough approximation based on region name.
        
        Args:
            polygon_file: PolygonFile model instance
            
        Returns:
            dict: Simple bounding box GeoJSON
        """
        # Very rough continent bounding boxes (for fallback only)
        fallback_boxes = {
            'africa': [[-20, -35], [55, -35], [55, 40], [-20, 40], [-20, -35]],
            'asia': [[25, -10], [150, -10], [150, 80], [25, 80], [25, -10]],
            'europe': [[-25, 35], [40, 35], [40, 72], [-25, 72], [-25, 35]],
            'north-america': [[-170, 15], [-50, 15], [-50, 75], [-170, 75], [-170, 15]],
            'south-america': [[-82, -56], [-34, -56], [-34, 13], [-82, 13], [-82, -56]],
            'oceania': [[110, -50], [180, -50], [180, -10], [110, -10], [110, -50]],
            'australia': [[110, -45], [155, -45], [155, -10], [110, -10], [110, -45]],
            'antarctica': [[-180, -90], [180, -90], [180, -60], [-180, -60], [-180, -90]]
        }
        
        # Try to find a matching bounding box
        name_lower = polygon_file.name.lower()
        for region, coords in fallback_boxes.items():
            if region in name_lower:
                return {
                    "type": "Polygon",
                    "coordinates": [coords]
                }
        
        # Ultimate fallback: small box at 0,0
        return {
            "type": "Polygon",
            "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]
        }
    
    @staticmethod
    def batch_generate_geojson(polygon_files_queryset) -> int:
        """
        Generate GeoJSON for multiple polygon files in batch.
        
        Args:
            polygon_files_queryset: QuerySet of PolygonFile objects
            
        Returns:
            int: Number of GeoJSON files generated
        """
        count = 0
        for polygon_file in polygon_files_queryset:
            try:
                PolygonGeoJsonService.get_or_generate_geojson(polygon_file)
                count += 1
            except Exception as e:
                logger.error(f"Failed to generate GeoJSON for {polygon_file.name}: {str(e)}")
                continue
        
        return count
