import logging
import numpy as np
from typing import Dict, List, Optional, Tuple
from collections import Counter
from django.db.models import Count, Avg, StdDev
from django.utils import timezone

from api.models import TemporalSnapshot, WorldKGClassDrift, WorldKGClassFingerprint
from worldkg_nca.models import OsmEntity
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service

logger = logging.getLogger(__name__)


class WorldKGDriftService:
    """
    Service for computing WorldKG class distribution drift over time.
    
    Provides:
    - Class distribution fingerprints per snapshot
    - Temporal drift metrics (KL/JS divergence, depth delta)
    - Semantic change detection
    """
    
    def __init__(self):
        self.ontology = get_worldkg_ontology_service()
    
    def compute_fingerprint(
        self,
        snapshot: TemporalSnapshot,
        processing_session=None
    ) -> WorldKGClassFingerprint:
        """
        Compute WorldKG class fingerprint for a temporal snapshot.
        
        Returns:
            WorldKGClassFingerprint instance
        """
        # Get all entities for this snapshot
        entities = OsmEntity.objects.using('vectors').filter(
            source_snapshot_id=snapshot.id,
            wkg_class__isnull=False
        )
        
        total_entities = entities.count()
        
        if total_entities == 0:
            logger.warning(f"No WorldKG-enriched entities for snapshot {snapshot.id}")
            # Return empty fingerprint
            return WorldKGClassFingerprint.objects.create(
                region=snapshot.region,
                snapshot=snapshot,
                class_distribution={},
                mean_depth=0.0,
                depth_std=0.0,
                unique_classes=0,
                shannon_entropy=0.0,
                simpson_index=0.0,
                top_5_classes={},
                total_entities=0,
                processing_session=processing_session
            )
        
        # Get class distribution
        class_counts = Counter(
            entities.values_list('wkg_class', flat=True)
        )
        
        # Normalize to percentages
        class_distribution = {
            cls: (count / total_entities) * 100
            for cls, count in class_counts.items()
        }
        
        # Compute depth statistics
        depth_stats = entities.aggregate(
            mean_depth=Avg('wkg_depth'),
            depth_std=StdDev('wkg_depth')
        )
        
        # Compute diversity metrics
        shannon_entropy = self._compute_shannon_entropy(class_distribution)
        simpson_index = self._compute_simpson_index(class_distribution)
        
        # Top 5 classes
        top_5 = dict(
            sorted(class_distribution.items(), key=lambda x: x[1], reverse=True)[:5]
        )
        
        fingerprint, _created = WorldKGClassFingerprint.objects.update_or_create(
            region=snapshot.region,
            snapshot=snapshot,
            defaults={
                'class_distribution': class_distribution,
                'mean_depth': depth_stats['mean_depth'] or 0.0,
                'depth_std': depth_stats['depth_std'] or 0.0,
                'unique_classes': len(class_counts),
                'shannon_entropy': shannon_entropy,
                'simpson_index': simpson_index,
                'top_5_classes': top_5,
                'total_entities': total_entities,
                'processing_session': processing_session,
            }
        )
        
        logger.info(
            f"Created fingerprint for {snapshot.region} @ {snapshot.timestamp}: "
            f"{total_entities} entities, {len(class_counts)} unique classes"
        )
        
        return fingerprint
    
    def compute_drift(
        self,
        snapshot_from: TemporalSnapshot,
        snapshot_to: TemporalSnapshot,
        bbox: Optional[List[float]] = None,
        processing_session=None
    ) -> WorldKGClassDrift:
        """
        Compute WorldKG class drift between two snapshots.
        
        Args:
            snapshot_from: Earlier snapshot
            snapshot_to: Later snapshot
            bbox: Optional bounding box [min_lon, min_lat, max_lon, max_lat]
            processing_session: Optional processing session
        
        Returns:
            WorldKGClassDrift instance
        """
        # Get fingerprints (create if not exist)
        fp_from = self._get_or_create_fingerprint(snapshot_from, processing_session)
        fp_to = self._get_or_create_fingerprint(snapshot_to, processing_session)
        
        dist_from = fp_from.class_distribution
        dist_to = fp_to.class_distribution
        
        # Compute divergence metrics
        kl_div = self._compute_kl_divergence(dist_from, dist_to)
        js_div = self._compute_js_divergence(dist_from, dist_to)
        
        # Depth delta
        depth_delta = fp_to.mean_depth - fp_from.mean_depth
        
        # Class changes
        classes_from = set(dist_from.keys())
        classes_to = set(dist_to.keys())
        
        new_classes = list(classes_to - classes_from)
        lost_classes = list(classes_from - classes_to)
        
        # Dominant shift (largest proportion changes)
        dominant_shift = self._compute_dominant_shift(dist_from, dist_to)
        
        # Categorize drift magnitude
        if js_div < 0.1:
            drift_magnitude = 'low'
        elif js_div < 0.3:
            drift_magnitude = 'medium'
        elif js_div < 0.5:
            drift_magnitude = 'high'
        else:
            drift_magnitude = 'extreme'
        
        drift = WorldKGClassDrift.objects.create(
            region=snapshot_from.region,
            bbox=bbox or [],
            snapshot_from=snapshot_from,
            snapshot_to=snapshot_to,
            class_distribution_from=dist_from,
            class_distribution_to=dist_to,
            kl_divergence=kl_div,
            js_divergence=js_div,
            depth_delta=depth_delta,
            new_classes=new_classes,
            lost_classes=lost_classes,
            dominant_shift=dominant_shift,
            drift_magnitude=drift_magnitude,
            processing_session=processing_session
        )
        
        logger.info(
            f"Computed drift for {snapshot_from.region}: "
            f"JS={js_div:.3f}, depth_delta={depth_delta:.2f}, magnitude={drift_magnitude}"
        )
        
        return drift
    
    def _get_or_create_fingerprint(
        self,
        snapshot: TemporalSnapshot,
        processing_session=None
    ) -> WorldKGClassFingerprint:
        """Get existing fingerprint or create new one."""
        fingerprint = WorldKGClassFingerprint.objects.filter(
            snapshot=snapshot
        ).first()
        
        if fingerprint is None:
            fingerprint = self.compute_fingerprint(snapshot, processing_session)
        
        return fingerprint
    
    def _compute_shannon_entropy(self, distribution: Dict[str, float]) -> float:
        """
        Compute Shannon entropy of class distribution.
        
        H = -Σ(p_i * log2(p_i))
        
        Higher entropy = more diverse (semantically heterogeneous)
        """
        if not distribution:
            return 0.0
        
        # Convert percentages to probabilities
        probs = np.array([v / 100.0 for v in distribution.values()])
        probs = probs[probs > 0]  # Remove zeros
        
        return -np.sum(probs * np.log2(probs))
    
    def _compute_simpson_index(self, distribution: Dict[str, float]) -> float:
        """
        Compute Simpson's diversity index.
        
        D = 1 - Σ(p_i^2)
        
        Ranges from 0 (no diversity) to ~1 (high diversity)
        """
        if not distribution:
            return 0.0
        
        # Convert percentages to probabilities
        probs = np.array([v / 100.0 for v in distribution.values()])
        
        return 1.0 - np.sum(probs ** 2)
    
    def _compute_kl_divergence(
        self,
        dist_p: Dict[str, float],
        dist_q: Dict[str, float]
    ) -> float:
        """
        Compute KL divergence: KL(P || Q) = Σ P(x) * log(P(x) / Q(x))
        
        Note: KL divergence is asymmetric.
        """
        if not dist_p or not dist_q:
            return 0.0
        
        # Get union of classes
        all_classes = set(dist_p.keys()) | set(dist_q.keys())
        
        # Add smoothing to avoid log(0)
        epsilon = 1e-10
        
        kl = 0.0
        for cls in all_classes:
            p = (dist_p.get(cls, 0.0) / 100.0) + epsilon
            q = (dist_q.get(cls, 0.0) / 100.0) + epsilon
            kl += p * np.log(p / q)
        
        return float(kl)
    
    def _compute_js_divergence(
        self,
        dist_p: Dict[str, float],
        dist_q: Dict[str, float]
    ) -> float:
        """
        Compute Jensen-Shannon divergence (symmetric version of KL).
        
        JS(P || Q) = 0.5 * KL(P || M) + 0.5 * KL(Q || M)
        where M = 0.5 * (P + Q)
        """
        if not dist_p or not dist_q:
            return 0.0
        
        # Get union of classes
        all_classes = set(dist_p.keys()) | set(dist_q.keys())
        
        # Compute mixture distribution M
        dist_m = {}
        for cls in all_classes:
            p_val = dist_p.get(cls, 0.0)
            q_val = dist_q.get(cls, 0.0)
            dist_m[cls] = 0.5 * (p_val + q_val)
        
        # Compute JS divergence
        kl_pm = self._compute_kl_divergence(dist_p, dist_m)
        kl_qm = self._compute_kl_divergence(dist_q, dist_m)
        
        return 0.5 * kl_pm + 0.5 * kl_qm
    
    def _compute_dominant_shift(
        self,
        dist_from: Dict[str, float],
        dist_to: Dict[str, float],
        top_n: int = 5
    ) -> Dict[str, float]:
        """
        Compute largest class proportion changes.
        
        Returns top N classes with largest absolute percentage change.
        """
        all_classes = set(dist_from.keys()) | set(dist_to.keys())
        
        deltas = {}
        for cls in all_classes:
            from_pct = dist_from.get(cls, 0.0)
            to_pct = dist_to.get(cls, 0.0)
            deltas[cls] = to_pct - from_pct
        
        # Sort by absolute change
        sorted_deltas = sorted(
            deltas.items(),
            key=lambda x: abs(x[1]),
            reverse=True
        )
        
        return dict(sorted_deltas[:top_n])


# Singleton instance
_drift_service = None

def get_worldkg_drift_service() -> WorldKGDriftService:
    """Get singleton instance of WorldKG drift service."""
    global _drift_service
    if _drift_service is None:
        _drift_service = WorldKGDriftService()
    return _drift_service
