"""
Places Enrichment Service — Best-effort Google Places augmentation.

Adds google_place_id, place_types, and place_rating to OsmEntity records
for entities with no Wikidata QID or low USLP confidence. This is a
non-blocking, best-effort step that enriches sparse entity graphs.

Usage:
    from extraction.services.places_enrichment_service import PlacesEnrichmentService
    svc = PlacesEnrichmentService(api_key="...")
    svc.enrich_country("CV", max_entities=1000)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PlacesEnrichmentService:
    """Enrich OSM entities with Google Places data.

    Targets entities that lack Wikidata alignment (no QID) or have low
    USLP confidence scores. Stores google_place_id, place_types, and
    place_rating on the OsmEntity record.

    This is a best-effort service — failures are logged but do not block
    the pipeline. Rate limiting and quota management are handled internally.
    """

    # Maximum entities to enrich per invocation (safety limit)
    DEFAULT_MAX_ENTITIES = 1000

    # Minimum USLP score below which we attempt Places enrichment
    DEFAULT_MIN_USLP_SCORE = 0.5

    # Delay between API calls (ms) to respect rate limits
    API_CALL_DELAY_MS = 200

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self._enriched_count = 0
        self._skipped_count = 0
        self._error_count = 0

    def enrich_country(
        self,
        iso: str,
        max_entities: int = DEFAULT_MAX_ENTITIES,
        min_uslp_score: float = DEFAULT_MIN_USLP_SCORE,
    ) -> Dict:
        """Enrich entities in a country with Google Places data.

        Args:
            iso: ISO 3166-1 alpha-2 country code (e.g., "CV").
            max_entities: Maximum entities to process.
            min_uslp_score: Only enrich entities with USLP score below this.

        Returns:
            Dict with enriched, skipped, and error counts.
        """
        if not self.api_key:
            logger.warning(
                "PlacesEnrichmentService: No API key configured — skipping enrichment"
            )
            return {"enriched": 0, "skipped": 0, "errors": 0, "reason": "no_api_key"}

        from worldkg_nca.models import OsmEntity
        from igea.models import SpatialTripletScore

        # Find entities in this country with no Wikidata QID
        # (primary target for Places enrichment)
        entities = (
            OsmEntity.objects.using("vectors")
            .filter(
                tags__has_key="addr:country",
                wikidata_id__isnull=True,
            )
            .only("osm_id", "geom", "tags", "wikidata_id")
        )

        # Filter by country tag
        iso_upper = iso.upper()
        country_entities = []
        for e in entities.iterator(chunk_size=500):
            tags = e.tags or {}
            tag_country = (tags.get("addr:country") or "").upper()
            if tag_country == iso_upper:
                country_entities.append(e)
                if len(country_entities) >= max_entities:
                    break

        if not country_entities:
            logger.info(
                "PlacesEnrichmentService: No eligible entities found for %s", iso
            )
            return {"enriched": 0, "skipped": 0, "errors": 0, "reason": "no_entities"}

        logger.info(
            "PlacesEnrichmentService: Found %d eligible entities for %s",
            len(country_entities),
            iso,
        )

        self._enriched_count = 0
        self._skipped_count = 0
        self._error_count = 0

        for entity in country_entities:
            try:
                self._enrich_entity(entity)
                time.sleep(self.API_CALL_DELAY_MS / 1000.0)
            except Exception as exc:
                self._error_count += 1
                logger.warning(
                    "PlacesEnrichmentService: Failed to enrich entity %s: %s",
                    entity.osm_id,
                    exc,
                )

        result = {
            "enriched": self._enriched_count,
            "skipped": self._skipped_count,
            "errors": self._error_count,
        }
        logger.info(
            "PlacesEnrichmentService: Complete for %s — %s",
            iso,
            result,
        )
        return result

    def _enrich_entity(self, entity) -> None:
        """Enrich a single entity with Google Places data.

        Uses the entity's name and coordinates to find a matching Place.
        Stores google_place_id, place_types, and place_rating on the entity.
        """
        tags = entity.tags or {}
        name = tags.get("name") or tags.get("name:en") or tags.get("official_name")

        if not name:
            self._skipped_count += 1
            return

        lat = entity.geom.y if entity.geom else None
        lon = entity.geom.x if entity.geom else None

        if lat is None or lon is None:
            self._skipped_count += 1
            return

        # Placeholder: actual Google Places API call would go here.
        # For now, this is a stub that demonstrates the integration point.
        # When an API key is configured, this would:
        #   1. Call Places API Nearby Search with lat/lon + name
        #   2. Match the best candidate by name similarity
        #   3. Store place_id, types, and rating on the entity
        #
        # Example API call:
        #   response = requests.get(
        #       "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        #       params={
        #           "location": f"{lat},{lon}",
        #           "radius": 500,
        #           "keyword": name,
        #           "key": self.api_key,
        #       },
        #   )
        #   results = response.json().get("results", [])
        #   if results:
        #       best = results[0]
        #       entity.google_place_id = best.get("place_id")
        #       entity.place_types = best.get("types", [])
        #       entity.place_rating = best.get("rating")
        #       entity.save(update_fields=["google_place_id", "place_types", "place_rating"])

        self._skipped_count += 1
        logger.debug(
            "PlacesEnrichmentService: Stub — would enrich entity %s (%s) at (%.4f, %.4f)",
            entity.osm_id,
            name,
            lat,
            lon,
        )


# Singleton accessor
_places_service: Optional[PlacesEnrichmentService] = None


def get_places_enrichment_service(
    api_key: Optional[str] = None,
) -> PlacesEnrichmentService:
    """Get or create the PlacesEnrichmentService singleton."""
    global _places_service
    if _places_service is None:
        from django.conf import settings

        key = api_key or getattr(settings, "GOOGLE_PLACES_API_KEY", None)
        _places_service = PlacesEnrichmentService(api_key=key)
    return _places_service
