#!/usr/bin/env python3
"""
Audit & populate bounding boxes for all CountryPipelineProfiles.

This script:
  1. Iterates over ALL CountryPipelineProfile records
  2. Calls populate_bbox_for_profile() on each (which resolves from 
     country_poly → OsmiumDatasetMetrics → resolve_country_bbox chain)
  3. Audits the stored bbox for sanity (min 0.5° span)
  4. Dumps a CSV to stdout with the results

Usage:
  cd backend
  DJANGO_SETTINGS_MODULE=backend.settings python ../scripts/audit_populate_bbox.py [--csv output.csv] [--dry-run]

Examples:
  # Just print to terminal
  DJANGO_SETTINGS_MODULE=backend.settings python ../scripts/audit_populate_bbox.py

  # Save CSV
  DJANGO_SETTINGS_MODULE=backend.settings python ../scripts/audit_populate_bbox.py --csv bbox_audit.csv

  # Dry-run (don't persist changes, just report current state)
  DJANGO_SETTINGS_MODULE=backend.settings python ../scripts/audit_populate_bbox.py --dry-run

Output CSV columns:
  iso2, iso3, canonical_name, canonical_slug, has_embeddings, 
  stored_bbox_min_lon, stored_bbox_min_lat, stored_bbox_max_lon, stored_bbox_max_lat,
  lon_span_deg, lat_span_deg, bbox_sane, has_country_poly, has_pbf_metrics, 
  entity_count_in_bbox, failure_reason
"""

import os
import sys
import csv
import argparse
from datetime import datetime

# Setup Django
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

import django
django.setup()

from django.db import connection, connections
from orchestration.models import CountryPipelineProfile
from extraction.services.osm_wikidata_resolver import populate_bbox_for_profile, resolve_country_bbox


MIN_BBOX_SPAN = 0.5  # degrees — same as resolve_country_bbox validation gate


def audit_populate_bbox(writer, dry_run: bool = False):
    """
    Iterate all profiles, populate bbox, audit, and write rows to CSV writer.
    """
    profiles = CountryPipelineProfile.objects.all().order_by("canonical_slug")
    total = profiles.count()

    print(f"Found {total} CountryPipelineProfile records", file=sys.stderr)

    for idx, profile in enumerate(profiles, 1):
        iso2 = profile.iso2 or ""
        iso3 = profile.iso3 or ""
        name = profile.canonical_name or ""
        slug = profile.canonical_slug or ""

        # Get current stored bbox
        payload = getattr(profile, "country_relations_payload", None) or {}
        stored_bbox = payload.get("bbox", {})

        # Try to populate/resolve bbox (unless dry run)
        bbox = None
        failure_reason = ""
        has_country_poly = "NO"
        has_pbf_metrics = "NO"

        if not dry_run:
            try:
                # Check if country_poly exists
                if profile.country_poly and getattr(profile.country_poly, "file_path", None):
                    has_country_poly = "YES"

                # Check if OsmiumDatasetMetrics exist for this profile's PBF
                if profile.country_pbf is not None:
                    from orchestration.models import OsmiumDatasetMetrics
                    metrics = (
                        OsmiumDatasetMetrics.objects.filter(pbf_file=profile.country_pbf)
                        .order_by("-metrics_generated_at")
                        .first()
                    )
                    if metrics and metrics.bounding_box:
                        has_pbf_metrics = "YES"

                bbox = populate_bbox_for_profile(profile)
            except Exception as exc:
                failure_reason = str(exc)[:200]
                bbox = None
        else:
            # Dry run: just check what's currently stored + what resolve would return
            bbox = resolve_country_bbox(iso2 or iso3)

            # Check poly/metrics existence
            if profile.country_poly and getattr(profile.country_poly, "file_path", None):
                has_country_poly = "YES"
            if profile.country_pbf is not None:
                from orchestration.models import OsmiumDatasetMetrics
                metrics = (
                    OsmiumDatasetMetrics.objects.filter(pbf_file=profile.country_pbf)
                    .order_by("-metrics_generated_at")
                    .first()
                )
                if metrics and metrics.bounding_box:
                    has_pbf_metrics = "YES"

        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
        else:
            min_lon = stored_bbox.get("min_lon", "")
            min_lat = stored_bbox.get("min_lat", "")
            max_lon = stored_bbox.get("max_lon", "")
            max_lat = stored_bbox.get("max_lat", "")

        # Sanity check
        bbox_sane = False
        lon_span = ""
        lat_span = ""
        entity_count = ""

        if bbox or stored_bbox:
            try:
                mlon = float(min_lon) if min_lon else None
                mlat = float(min_lat) if min_lat else None
                xlon = float(max_lon) if max_lon else None
                xlat = float(max_lat) if max_lat else None

                if all(v is not None for v in [mlon, mlat, xlon, xlat]):
                    lon_span = round(xlon - mlon, 4)
                    lat_span = round(xlat - mlat, 4)
                    bbox_sane = lon_span >= MIN_BBOX_SPAN and lat_span >= MIN_BBOX_SPAN

                    # Count entities in bbox from vectors DB
                    try:
                        from django.contrib.gis.geos import Polygon
                        polygon = Polygon.from_bbox((mlon, mlat, xlon, xlat))
                        from worldkg_nca.models import OsmEntity
                        entity_count = OsmEntity.objects.using("vectors").filter(
                            geom__within=polygon,
                            gv_tags_embedding__isnull=False,
                        ).count()
                    except Exception as exc:
                        entity_count = f"ERR:{str(exc)[:50]}"
            except (TypeError, ValueError):
                pass

        row = {
            "iso2": iso2,
            "iso3": iso3,
            "canonical_name": name,
            "canonical_slug": slug,
            "has_embeddings": "YES" if profile.has_embeddings else "NO",
            "stored_bbox_min_lon": min_lon,
            "stored_bbox_min_lat": min_lat,
            "stored_bbox_max_lon": max_lon,
            "stored_bbox_max_lat": max_lat,
            "lon_span_deg": lon_span,
            "lat_span_deg": lat_span,
            "bbox_sane": "YES" if bbox_sane else "NO",
            "has_country_poly": has_country_poly,
            "has_pbf_metrics": has_pbf_metrics,
            "entity_count_in_bbox": entity_count,
            "failure_reason": failure_reason,
        }

        writer.writerow(row)

        # Log progress
        status = "✅" if bbox_sane else "❌"
        print(
            f"[{idx}/{total}] {status} {iso2:4s} {name:30s} "
            f"bbox={min_lon},{min_lat},{max_lon},{max_lat} "
            f"span={lon_span}°x{lat_span}° "
            f"entities={entity_count}",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Audit and populate bounding boxes for all CountryPipelineProfiles"
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Path to output CSV file (default: print to stdout)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Don't persist bbox changes, just report current state",
    )
    args = parser.parse_args()

    fieldnames = [
        "iso2",
        "iso3",
        "canonical_name",
        "canonical_slug",
        "has_embeddings",
        "stored_bbox_min_lon",
        "stored_bbox_min_lat",
        "stored_bbox_max_lon",
        "stored_bbox_max_lat",
        "lon_span_deg",
        "lat_span_deg",
        "bbox_sane",
        "has_country_poly",
        "has_pbf_metrics",
        "entity_count_in_bbox",
        "failure_reason",
    ]

    if args.csv:
        f = open(args.csv, "w", newline="")
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()

    mode = "DRY-RUN" if args.dry_run else "POPULATE"
    print(
        f"\n{'='*60}\n"
        f"Bbox Audit ({mode} mode)\n"
        f"Started: {datetime.now().isoformat()}\n"
        f"{'='*60}",
        file=sys.stderr,
    )

    audit_populate_bbox(writer, dry_run=args.dry_run)

    print(
        f"\n{'='*60}\n"
        f"Completed: {datetime.now().isoformat()}\n"
        f"{'='*60}",
        file=sys.stderr,
    )

    if args.csv:
        f.close()
        print(f"\nCSV written to {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
