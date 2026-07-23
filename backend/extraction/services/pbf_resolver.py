import logging
import uuid
from typing import Any, Optional
from api.models import PbfFile, RegionHierarchy

logger = logging.getLogger(__name__)

class PBFResolver:
    """
    Centralized utility to resolve PbfFile objects from various inputs:
    - PbfFile UUID (as string or uuid.UUID)
    - RegionHierarchy UUID (as string or uuid.UUID)
    - Region Name (case-insensitive)
    - Direct PbfFile object
    """
    
    @staticmethod
    def resolve(source_input: Any) -> Optional[PbfFile]:
        if not source_input:
            return None
            
        # 1. Already a PbfFile object
        if isinstance(source_input, PbfFile):
            return source_input
            
        # 2. String or UUID input
        if isinstance(source_input, (str, uuid.UUID)):
            incoming_id = str(source_input)
            
            # Try PbfFile first
            pbf = PbfFile.objects.filter(id=incoming_id).first()
            if pbf:
                return pbf
                
            # If not found, check if it's a RegionHierarchy ID
            region = RegionHierarchy.objects.filter(id=incoming_id).first()
            if not region:
                # Fallback to name search as a last resort
                region = RegionHierarchy.objects.filter(name__iexact=incoming_id).first()
            
            if region:
                if region.corresponding_pbf:
                    logger.info(f"Resolved Region {region.name} to PBF: {region.corresponding_pbf.id}")
                    return region.corresponding_pbf
                else:
                    logger.warning(f"Region {region.name} found but has no corresponding PBF yet.")
                    return None
                    
        return None

pbf_resolver = PBFResolver()
