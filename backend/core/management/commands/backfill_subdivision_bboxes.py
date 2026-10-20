"""Backfill subdivision bboxes on SubgraphProfile.

The pipeline harvests the P150/P402 hierarchy from SPARQL, and the
SPARQL endpoint does not always return the boundary/QID data for every
subdivision — so a SubgraphProfile can carry a QID + OSM relation id
without a populated bbox (observed 2026-09-30: 4 of 209 — the Dutch
Caribbean islands + Toledo District). Without a bbox,
``resolve_subdivision_bbox`` returns None and subdivision-scoped sample
questions/search cannot run.

This command fills every missing bbox from the best available source,
in order:

1. ``OsmBoundary`` by ``osm_id`` (the ingested admin boundary table).
2. Nominatim lookup by OSM relation id (``osm_ids=R<id>`` →
   ``boundingbox``) — deterministic, no key.

``--check`` reports the gaps without writing and exits 1 when any
remain (the release gate: run it before shipping, like
``verify_paths --strict``). Fail-soft: a network error keeps the row a
gap and is logged.
"""

import json
import logging
import time

import requests
from django.core.management.base import BaseCommand

from core.models import OsmBoundary, SubgraphProfile
from django.db.models import Q

logger = logging.getLogger(__name__)

_NOMINATIM_LOOKUP = "https://nominatim.openstreetmap.org/lookup"
_USER_AGENT = "worldkg-backfill/1.0 (dev)"


class Command(BaseCommand):
    help = (
        "Backfill missing subdivision bboxes (OsmBoundary first, then "
        "Nominatim by OSM relation id); --check reports gaps and exits 1."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Report gaps without writing; exit 1 when any remain.",
        )

    def handle(self, *args, **options):
        gaps = self._missing_bbox_rows()
        if not gaps:
            self.stdout.write(self.style.SUCCESS(
                "All subdivisions have bboxes populated."
            ))
            return

        if options["check"]:
            self.stdout.write(self.style.ERROR(
                f"{len(gaps)} subdivisions missing a bbox:"
            ))
            for sg in gaps:
                self.stdout.write(
                    f"  {sg.name} ({sg.country_profile.iso2}) "
                    f"qid={sg.wikidata_id} rel={sg.osm_relation_id}"
                )
            raise SystemExit(1)

        filled, still_missing = 0, []
        for sg in gaps:
            result = self._bbox_for(sg)
            if result is None:
                still_missing.append(sg)
                continue
            bbox, source = result
            (sg.bbox_min_lon, sg.bbox_min_lat,
             sg.bbox_max_lon, sg.bbox_max_lat) = bbox
            sg.bbox_source = source
            from django.utils import timezone
            sg.bbox_updated_at = timezone.now()
            sg.save(update_fields=[
                "bbox_min_lon", "bbox_min_lat",
                "bbox_max_lon", "bbox_max_lat",
                "bbox_source", "bbox_updated_at",
            ])
            filled += 1
            self.stdout.write(
                f"  ✓ {sg.name} ({sg.country_profile.iso2}) → "
                f"[{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}] "
                f"({source})"
            )
            time.sleep(1)  # Nominatim usage policy (~1 req/s)

        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {filled} bbox(es)."
        ))
        if still_missing:
            self.stdout.write(self.style.WARNING(
                f"{len(still_missing)} still missing: "
                + ", ".join(sg.name for sg in still_missing)
            ))
            raise SystemExit(1)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _missing_bbox_rows() -> list:
        return list(SubgraphProfile.objects.filter(
            wikidata_id__isnull=False,
        ).filter(
            Q(bbox_min_lon__isnull=True) | Q(bbox_max_lat__isnull=True),
        ).order_by("country_profile__iso2", "name"))

    @classmethod
    def _bbox_for(cls, sg) -> tuple:
        """((min_lon, min_lat, max_lon, max_lat), source) or None.

        ``source`` is 'osm_boundary' or 'nominatim' — stamped on the row so
        bbox provenance is first-class (``bbox_source``).
        """
        # 1. Ingested admin boundary by OSM relation id.
        if sg.osm_relation_id:
            boundary = OsmBoundary.objects.filter(
                osm_id=sg.osm_relation_id,
            ).exclude(bbox__isnull=True).first()
            if boundary and boundary.bbox and len(boundary.bbox) == 4:
                # OsmBoundary.bbox order matches (min_lon, min_lat, ...).
                return (
                    tuple(float(v) for v in boundary.bbox), "osm_boundary",
                )
        # 2. Nominatim lookup by relation id.
        if sg.osm_relation_id:
            bbox = cls._nominatim_bbox(sg.osm_relation_id)
            if bbox:
                return bbox, "nominatim"
        logger.warning(
            "No bbox source for %s (qid=%s rel=%s)",
            sg.name, sg.wikidata_id, sg.osm_relation_id,
        )
        return None

    @staticmethod
    def _nominatim_bbox(osm_relation_id) -> tuple:
        """Nominatim ``boundingbox`` [min_lat, max_lat, min_lon, max_lon]
        for an OSM relation, or None (fail-soft)."""
        try:
            resp = requests.get(
                _NOMINATIM_LOOKUP,
                params={"osm_ids": f"R{osm_relation_id}", "format": "json"},
                headers={"User-Agent": _USER_AGENT},
                timeout=(3.0, 15.0),
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Nominatim lookup failed for R%s: %s",
                           osm_relation_id, exc)
            return None
        for row in data or []:
            bb = row.get("boundingbox") or []
            if len(bb) == 4:
                try:
                    min_lat, max_lat, min_lon, max_lon = map(float, bb)
                except (TypeError, ValueError):
                    continue
                return (min_lon, min_lat, max_lon, max_lat)
        return None
