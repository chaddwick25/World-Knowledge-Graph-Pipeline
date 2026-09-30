"""
Google Places Validation Service

Validates low-confidence USLP spatial links using Google Geocoding API.
Implements the amplification layer described in docs/plans/validating_layer.md.

Architecture:
  USLP Rejected Links (normalized_score < 0.7)
      │
      ▼
  GooglePlacesValidator.validate_link()
      │
      ├── Validated (google_score ≥ 0.5) ──→ Accept + mark as training data
      │
      └── Rejected ──→ Remain rejected
"""

import logging
import time
from typing import Dict, Optional, Tuple
from django.conf import settings
logger = logging.getLogger(__name__)

class GooglePlacesUsageTracker:
    """Tracks Google API usage and costs for monitoring."""

    def __init__(self):
        self.call_count = 0
        self.total_cost = 0.0
        self.cost_per_call = 0.005  # Geocoding starter tier

    def record_call(self):
        self.call_count += 1
        self.total_cost += self.cost_per_call

    @property
    def summary(self) -> Dict:
        return {
            'call_count': self.call_count,
            'total_cost_usd': round(self.total_cost, 4),
        }


class GooglePlacesValidator:
    """
    Validates spatial links using Google Geocoding API.

    Only validates links when GOOGLE_VALIDATION_ENABLED=True and
    GOOGLE_API_KEY is configured. Otherwise operates as a no-op.
    """

    def __init__(self):
        self.enabled = getattr(settings, 'GOOGLE_VALIDATION_ENABLED', False)
        self.api_key = getattr(settings, 'GOOGLE_API_KEY', None)
        self.client = None
        self.usage_tracker = GooglePlacesUsageTracker()

        if self.enabled and self.api_key:
            try:
                import googlemaps
                self.client = googlemaps.Client(key=self.api_key)
                logger.info('GooglePlacesValidator initialized with API key')
            except ImportError:
                logger.warning(
                    'googlemaps package not installed. '
                    'Install with: pip install googlemaps'
                )
                self.enabled = False
            except Exception as exc:
                logger.warning(f'Failed to initialize Google Maps client: {exc}')
                self.enabled = False
        elif self.enabled and not self.api_key:
            logger.warning(
                'GOOGLE_VALIDATION_ENABLED=True but GOOGLE_API_KEY not set. '
                'Validation disabled.'
            )
            self.enabled = False

    def validate_link(
        self,
        head_name: str,
        head_lat: float,
        head_lon: float,
        tail_name: str,
        tail_lat: float,
        tail_lon: float,
        relation: str,
    ) -> Optional[float]:
        """
        Validate a spatial link using Google Geocoding.

        Strategy: Geocode the tail entity's name near the head entity's location.
        If Google finds a matching place nearby, the link is validated.

        Args:
            head_name: Name of the head entity
            head_lat: Latitude of head entity
            head_lon: Longitude of head entity
            tail_name: Name of the tail entity (candidate)
            tail_lat: Latitude of tail entity
            tail_lon: Longitude of tail entity
            relation: Spatial relation type

        Returns:
            Confidence score (0.0-1.0) or None if validation skipped/disabled
        """
        if not self.enabled or not self.client:
            return None

        try:
            self.usage_tracker.record_call()

            # Geocode the tail entity name, biased toward head location
            geocode_result = self.client.geocode(
                tail_name,
                location=(head_lat, head_lon),
                region=self._infer_region(head_lat, head_lon),
            )

            if not geocode_result:
                logger.debug(f'No geocode results for "{tail_name}"')
                return 0.0

            best = geocode_result[0]
            result_location = best['geometry']['location']
            result_lat = result_location['lat']
            result_lng = result_location['lng']

            # Compute distance between geocode result and expected tail location
            distance_km = self._haversine_km(
                tail_lat, tail_lon, result_lat, result_lng
            )

            # Score based on distance: closer = higher confidence
            if distance_km < 0.1:  # Within 100m — excellent match
                confidence = 1.0
            elif distance_km < 1.0:  # Within 1km — good match
                confidence = 0.8
            elif distance_km < 5.0:  # Within 5km — moderate match
                confidence = 0.6
            elif distance_km < 20.0:  # Within 20km — weak match
                confidence = 0.4
            else:
                confidence = 0.2

            # Boost if the place type matches the relation semantics
            place_types = best.get('types', [])
            if self._types_match_relation(place_types, relation):
                confidence = min(1.0, confidence + 0.1)

            logger.debug(
                f'Validated "{tail_name}": distance={distance_km:.2f}km, '
                f'confidence={confidence:.2f}'
            )
            return confidence

        except Exception as exc:
            logger.warning(f'Google validation failed for "{tail_name}": {exc}')
            return None

    def validate_batch(
        self,
        links: list,
        entity_name_resolver=None,
    ) -> Dict[str, float]:
        """
        Validate a batch of rejected links.

        Args:
            links: List of dicts with keys:
                - head_osm_id, tail_osm_id
                - relation
                - normalized_score
            entity_name_resolver: Optional callable(osm_id) -> (name, lat, lon)

        Returns:
            Dict mapping (head_id, tail_id, relation) -> google_confidence
        """
        results = {}
        if not self.enabled:
            return results

        for link in links:
            head_id = link.get('head_osm_id')
            tail_id = link.get('tail_osm_id')
            relation = link.get('relation', '')

            if entity_name_resolver:
                head_info = entity_name_resolver(head_id)
                tail_info = entity_name_resolver(tail_id)
                if not head_info or not tail_info:
                    continue
                head_name, head_lat, head_lon = head_info
                tail_name, tail_lat, tail_lon = tail_info
            else:
                continue

            score = self.validate_link(
                head_name, head_lat, head_lon,
                tail_name, tail_lat, tail_lon,
                relation,
            )

            if score is not None:
                key = (str(head_id), str(tail_id), relation)
                results[key] = score

            # Rate limiting: ~50 calls/sec for starter tier
            time.sleep(0.02)

        return results

    @staticmethod
    def _haversine_km(lat1, lon1, lat2, lon2):
        """Compute haversine distance in kilometers."""
        from math import asin, cos, radians, sin, sqrt

        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        return R * 2 * asin(sqrt(a))

    @staticmethod
    def _infer_region(lat, lon):
        """Infer a region hint from coordinates (simple heuristic)."""
        # Rough continent mapping for geocoding bias
        if lon < -30:
            return 'us' if lat > 25 else 'br'
        elif lon < 60:
            return 'gb'
        else:
            return ''

    @staticmethod
    def _types_match_relation(place_types, relation):
        """Check if Google place types match the spatial relation semantics."""
        relation_type_map = {
            'near': {'point_of_interest', 'establishment', 'premise'},
            'within': {'neighborhood', 'sublocality', 'locality'},
            'contains': {'locality', 'administrative_area_level_3',
                         'administrative_area_level_2'},
            'adjacent': {'route', 'street_address'},
        }
        expected = relation_type_map.get(relation, set())
        return bool(expected & set(place_types))
