#!/usr/bin/env python3
"""
Audit script to check which countries have polygon files but aren't appearing on the Recipe Builder map.
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from extraction.models import RegionHierarchy, PolygonFile, OsmBoundary
from extraction.services.polygon_geojson_service import PolygonGeoJsonService
import json

def audit_countries():
    """Audit which countries should be clickable on the map."""
    
    print("=" * 80)
    print("RECIPE BUILDER COUNTRIES AUDIT")
    print("=" * 80)
    
    # 1. Check all RegionHierarchy nodes (countries are level 2 in hierarchy)
    print("\n1. REGION HIERARCHY - All regions in database:")
    all_regions = RegionHierarchy.objects.all().order_by('name')
    print(f"   Total regions in RegionHierarchy: {all_regions.count()}")
    
    # Identify countries (nodes with a continent parent)
    continents = RegionHierarchy.objects.filter(parent__isnull=True)
    countries = RegionHierarchy.objects.filter(parent__in=continents)
    print(f"   Continents: {continents.count()}")
    print(f"   Countries (children of continents): {countries.count()}")
    
    # 2. Check which have polygon files
    print("\n2. POLYGON FILES - Countries with polygon files:")
    countries_with_poly = countries.filter(poly_file_path__isnull=False).exclude(poly_file_path='')
    print(f"   Countries with polygon files: {countries_with_poly.count()}")
    
    # 3. Check which have OSM boundaries
    print("\n3. OSM BOUNDARIES - Countries with OSM boundaries:")
    countries_with_osm = []
    for country in countries:
        boundary = OsmBoundary.objects.filter(
            name__iexact=country.name,
            admin_level=2
        ).first()
        if boundary:
            countries_with_osm.append(country.name)
    print(f"   Countries with OSM boundaries: {len(countries_with_osm)}")
    
    # 4. Check which countries should be on map but might have issues
    print("\n4. COUNTRIES WITH ISSUES:")
    print("   Checking GeoJSON generation for countries with polygon files...")
    
    issues = []
    geojson_service = PolygonGeoJsonService()
    
    for country in countries_with_poly:
        # Check if OSM boundary exists
        osm_boundary = OsmBoundary.objects.filter(
            name__iexact=country.name,
            admin_level=2
        ).first()
        
        # Check if polygon file GeoJSON can be generated
        poly_geojson = None
        if country.poly_file_path:
            try:
                poly_geojson = geojson_service.get_geojson(country.poly_file_path)
            except Exception as e:
                issues.append({
                    'country': country.name,
                    'issue': f'Polygon GeoJSON generation failed: {str(e)}',
                    'has_osm_boundary': osm_boundary is not None,
                    'poly_file_path': country.poly_file_path
                })
                continue
        
        # If no OSM boundary and no valid polygon GeoJSON, it won't be clickable
        if not osm_boundary and not poly_geojson:
            issues.append({
                'country': country.name,
                'issue': 'No OSM boundary and no valid polygon GeoJSON',
                'has_osm_boundary': False,
                'poly_file_path': country.poly_file_path
            })
    
    if issues:
        print(f"\n   Found {len(issues)} countries with issues:")
        for issue in issues:
            print(f"\n   ❌ {issue['country']}")
            print(f"      Issue: {issue['issue']}")
            print(f"      Has OSM boundary: {issue['has_osm_boundary']}")
            print(f"      Poly file: {issue['poly_file_path']}")
    else:
        print("\n   ✓ No issues found - all countries with polygon files should be clickable")
    
    # 5. List specific countries to check
    print("\n5. SPECIFIC COUNTRY CHECK:")
    test_countries = ['France', 'Germany', 'United Kingdom', 'Italy', 'Spain', 'Canada', 'United States']
    
    for country_name in test_countries:
        country = RegionHierarchy.objects.filter(name__iexact=country_name, region_type='country').first()
        if country:
            osm_boundary = OsmBoundary.objects.filter(name__iexact=country_name, admin_level=2).first()
            has_poly = bool(country.poly_file_path)
            
            status = "✓" if (osm_boundary or has_poly) else "❌"
            print(f"   {status} {country_name}:")
            print(f"      - OSM Boundary: {'Yes' if osm_boundary else 'No'}")
            print(f"      - Polygon File: {'Yes' if has_poly else 'No'}")
            if has_poly:
                print(f"      - Path: {country.poly_file_path}")
        else:
            print(f"   ❌ {country_name}: Not found in RegionHierarchy")
    
    # 6. Summary
    print("\n" + "=" * 80)
    print("SUMMARY:")
    print(f"  Total countries in database: {countries.count()}")
    print(f"  Countries with polygon files: {countries_with_poly.count()}")
    print(f"  Countries with OSM boundaries: {len(countries_with_osm)}")
    print(f"  Countries with issues: {len(issues)}")
    print("=" * 80)
    
    return issues

if __name__ == '__main__':
    issues = audit_countries()
    sys.exit(0 if len(issues) == 0 else 1)
