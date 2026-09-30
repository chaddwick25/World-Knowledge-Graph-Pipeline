"""
PBF Hierarchy Resolver Service

Finds the optimal source PBF file for extraction by traversing the region hierarchy
and selecting the closest available parent with history data.
"""
from typing import Optional, Tuple
from pathlib import Path


class PBFHierarchyResolver:
    """
    Resolves the optimal source PBF file for extraction based on region hierarchy.
    
    Instead of always using the planet file, this service finds the closest parent
    region that has an available PBF file with history data.
    """
    
    def find_optimal_source_pbf(
        self, 
        poly_file_path: str, 
        fallback_planet_pbf = None
    ) -> Tuple:
        """
        Find the optimal source PBF file for extracting a region.
        
        Algorithm:
        1. Find the RegionHierarchy node for the target poly file
        2. Traverse up the hierarchy (target -> parent -> grandparent -> ...)
        3. At each level, check if a PBF file exists with history data
        4. Return the first (closest) available PBF file
        5. Fallback to planet file if no parent PBF is found
        
        Args:
            poly_file_path: Path to the .poly file for the target region
            fallback_planet_pbf: Planet PBF file to use if no parent found
        
        Returns:
            Tuple of (PbfFile, reason_string)
            - PbfFile: The optimal source PBF to use
            - reason_string: Explanation of why this source was chosen
        
        Examples:
            >>> # Extracting australia.poly
            >>> # Hierarchy: planet -> oceania -> australia
            >>> # If oceania.pbf exists with history, use it instead of planet
            >>> resolver = PBFHierarchyResolver()
            >>> source, reason = resolver.find_optimal_source_pbf(
            ...     '/path/to/oceania/australia.poly'
            ... )
            >>> # Returns: (oceania.pbf, "Using parent region 'oceania' (1 level up)")
        """
        # Find the target region node
        from api.models import RegionHierarchy, PbfFile
        target_region = self._find_region_by_poly_path(poly_file_path)
        
        if not target_region:
            reason = f"No RegionHierarchy found for {Path(poly_file_path).name}"
            if fallback_planet_pbf:
                return fallback_planet_pbf, f"{reason}, using planet file"
            raise ValueError(f"{reason} and no fallback planet file provided")
        
        # Traverse up the hierarchy to find available PBF files
        current_node = target_region.parent
        levels_up = 1
        
        while current_node:
            # Check if this parent has a corresponding PBF file
            if current_node.corresponding_pbf:
                pbf = current_node.corresponding_pbf
                
                # Verify the PBF file exists and has history
                if self._is_pbf_valid_for_extraction(pbf):
                    reason = (
                        f"Using parent region '{current_node.name}' "
                        f"({levels_up} level{'s' if levels_up > 1 else ''} up, "
                        f"{pbf.size_bytes / (1024**3):.2f}GB)"
                    )
                    return pbf, reason
            
            # Move up one level
            current_node = current_node.parent
            levels_up += 1
        
        # No suitable parent found, use planet file
        if fallback_planet_pbf:
            reason = (
                f"No parent PBF found for '{target_region.name}', "
                f"using planet file ({fallback_planet_pbf.size_bytes / (1024**3):.2f}GB)"
            )
            return fallback_planet_pbf, reason
        
        raise ValueError(
            f"No suitable source PBF found for '{target_region.name}' "
            f"and no fallback planet file provided"
        )
    
    def _find_region_by_poly_path(self, poly_file_path: str):
        """
        Find RegionHierarchy node by poly file path.
        
        Tries multiple matching strategies:
        1. Exact path match
        2. Filename match (case-insensitive)
        3. Stem match (without extension)
        """
        # Try exact path match first
        from api.models import RegionHierarchy
        region = RegionHierarchy.objects.filter(poly_file_path=poly_file_path).first()
        if region:
            return region
        
        # Try filename match (case-insensitive)
        poly_filename = Path(poly_file_path).name
        region = RegionHierarchy.objects.filter(
            poly_file_path__iendswith=poly_filename
        ).first()
        if region:
            return region
        
        # Try stem match (e.g., "australia.poly" -> "australia")
        poly_stem = Path(poly_file_path).stem
        region = RegionHierarchy.objects.filter(
            poly_file_path__icontains=poly_stem
        ).first()
        
        return region
    
    def _is_pbf_valid_for_extraction(self, pbf) -> bool:
        """
        Check if a PBF file is valid for use as an extraction source.
        
        Requirements:
        1. File must exist on disk
        2. Must have history data (for temporal analysis)
        3. Status must be COMPLETED
        4. File size must be > 0
        """
        if not pbf:
            return False
        
        # Check status
        from api.models import PbfFile
        if pbf.status != PbfFile.PbfStatus.COMPLETED:
            return False
        
        # Check has history
        if not pbf.has_history:
            return False
        
        # Check file exists
        if not Path(pbf.path).exists():
            return False
        
        # Check file size
        if pbf.size_bytes <= 0:
            return False
        
        return True
    
    def get_hierarchy_path(self, region) -> list:
        """
        Get the full hierarchy path from root to the given region.
        
        Args:
            region: Target RegionHierarchy node
        
        Returns:
            List of region names from root to target
            Example: ['planet', 'oceania', 'australia']
        """
        path = []
        current = region
        
        while current:
            path.insert(0, current.name)
            current = current.parent
        
        return path
    
    def estimate_extraction_speedup(
        self, 
        source_pbf, 
        planet_pbf
    ) -> float:
        """
        Estimate the speedup factor from using a smaller parent PBF.
        
        Assumes extraction time is roughly proportional to source file size.
        
        Args:
            source_pbf: The selected source PBF
            planet_pbf: The planet PBF file
        
        Returns:
            Speedup factor (e.g., 10.5 means ~10.5x faster)
        """
        if not source_pbf or not planet_pbf:
            return 1.0
        
        if source_pbf.size_bytes <= 0:
            return 1.0
        
        speedup = planet_pbf.size_bytes / source_pbf.size_bytes
        return speedup


# Singleton instance
pbf_hierarchy_resolver = PBFHierarchyResolver()
