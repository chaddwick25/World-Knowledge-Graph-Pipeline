"""
Extraction Chain Builder Service

Builds hierarchical extraction configurations similar to recipe scripts.
Automatically determines the extraction chain from planet down to target region.
"""

import logging
from typing import Dict, List, Optional
from core.models import RegionHierarchy, PbfFile, PolygonFile

logger = logging.getLogger(__name__)


class ExtractionChainBuilder:
    """Builds extraction chain configurations for hierarchical extraction"""
    
    @staticmethod
    def build_extraction_chain(target_region_id: str) -> Dict:
        """
        Build complete extraction chain from planet to target region.
        
        Similar to run_canada_recipe.py, this builds the chain:
        Planet → Continent → Country → Region → City
        
        Args:
            target_region_id: UUID of target region
            
        Returns:
            dict: Complete extraction configuration
        """
        try:
            target_region = RegionHierarchy.objects.get(id=target_region_id)
        except RegionHierarchy.DoesNotExist:
            raise ValueError(f"Region {target_region_id} not found")
        
        # Build ancestor chain
        chain = ExtractionChainBuilder._build_ancestor_chain(target_region)
        
        # Build extraction steps
        steps = ExtractionChainBuilder._build_extraction_steps(chain)
        
        # Calculate estimated resources
        resources = ExtractionChainBuilder._estimate_resources(steps)
        
        return {
            'target_region': {
                'id': str(target_region.id),
                'name': target_region.name,
                'level': ExtractionChainBuilder._determine_level(target_region)
            },
            'chain': chain,
            'steps': steps,
            'resources': resources,
            'total_steps': len(steps)
        }
    
    @staticmethod
    def _build_ancestor_chain(region: RegionHierarchy) -> List[Dict]:
        """
        Build chain from planet to target region.
        
        Returns list in order: [planet, continent, country, region, city]
        """
        chain = []
        current = region
        
        # Walk up the tree to root
        while current:
            chain.insert(0, {
                'id': str(current.id),
                'name': current.name,
                'level': ExtractionChainBuilder._determine_level(current),
                'polygon_file': current.polygon_file.file_path if current.polygon_file else None,
                'pbf_exists': current.corresponding_pbf is not None,
                'pbf_id': str(current.corresponding_pbf.id) if current.corresponding_pbf else None
            })
            current = current.parent
        
        # Add planet at the beginning if not present
        planet_pbf = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.PLANET,
            status=PbfFile.PbfStatus.COMPLETED
        ).first()
        
        if planet_pbf and (not chain or chain[0]['level'] != 'planet'):
            chain.insert(0, {
                'id': 'planet',
                'name': 'Planet',
                'level': 'planet',
                'polygon_file': None,
                'pbf_exists': True,
                'pbf_id': str(planet_pbf.id)
            })
        
        return chain
    
    @staticmethod
    def _build_extraction_steps(chain: List[Dict]) -> List[Dict]:
        """
        Build extraction steps from chain.
        
        Each step extracts from parent PBF using child's polygon file.
        """
        steps = []
        
        for i in range(1, len(chain)):
            parent = chain[i - 1]
            child = chain[i]
            
            # Skip if child already has PBF
            if child['pbf_exists']:
                steps.append({
                    'step_number': i,
                    'action': 'skip',
                    'reason': 'PBF already exists',
                    'source_region': parent['name'],
                    'target_region': child['name'],
                    'source_pbf_id': parent['pbf_id'],
                    'target_pbf_id': child['pbf_id']
                })
                continue
            
            # Check if parent has PBF
            if not parent['pbf_exists']:
                steps.append({
                    'step_number': i,
                    'action': 'error',
                    'reason': f"Parent region '{parent['name']}' has no PBF file",
                    'source_region': parent['name'],
                    'target_region': child['name'],
                    'requires': f"Extract {parent['name']} first"
                })
                continue
            
            # Check if child has polygon file
            if not child['polygon_file']:
                steps.append({
                    'step_number': i,
                    'action': 'error',
                    'reason': f"No polygon file for '{child['name']}'",
                    'source_region': parent['name'],
                    'target_region': child['name']
                })
                continue
            
            # Valid extraction step
            steps.append({
                'step_number': i,
                'action': 'extract',
                'source_region': parent['name'],
                'target_region': child['name'],
                'source_pbf_id': parent['pbf_id'],
                'polygon_file': child['polygon_file'],
                'estimated_time_minutes': ExtractionChainBuilder._estimate_extraction_time(
                    parent['level'],
                    child['level']
                )
            })
        
        return steps
    
    @staticmethod
    def _estimate_extraction_time(parent_level: str, child_level: str) -> int:
        """
        Estimate extraction time based on hierarchy levels.
        
        Based on empirical data:
        - Planet → Continent: ~90 minutes
        - Continent → Country: ~15 minutes
        - Country → Region: ~5 minutes
        - Region → City: ~2 minutes
        """
        time_matrix = {
            ('planet', 'continent'): 90,
            ('continent', 'country'): 15,
            ('country', 'region'): 5,
            ('region', 'city'): 2
        }
        
        return time_matrix.get((parent_level, child_level), 10)
    
    @staticmethod
    def _estimate_resources(steps: List[Dict]) -> Dict:
        """Estimate total resources needed for extraction chain"""
        total_time = sum(
            step.get('estimated_time_minutes', 0)
            for step in steps
            if step['action'] == 'extract'
        )
        
        extraction_count = sum(
            1 for step in steps if step['action'] == 'extract'
        )
        
        skip_count = sum(
            1 for step in steps if step['action'] == 'skip'
        )
        
        error_count = sum(
            1 for step in steps if step['action'] == 'error'
        )
        
        return {
            'total_time_minutes': total_time,
            'total_time_hours': round(total_time / 60, 1),
            'extraction_count': extraction_count,
            'skip_count': skip_count,
            'error_count': error_count,
            'can_execute': error_count == 0
        }
    
    @staticmethod
    def _determine_level(region: RegionHierarchy) -> str:
        """Determine hierarchical level of region"""
        depth = 0
        current = region
        while current.parent:
            depth += 1
            current = current.parent
        
        levels = {
            0: 'continent',
            1: 'country',
            2: 'region',
            3: 'city'
        }
        
        return levels.get(depth, 'subregion')
    
    @staticmethod
    def generate_recipe_config(target_region_id: str) -> Dict:
        """
        Generate complete recipe configuration similar to recipe scripts.
        
        This generates a config that can be executed like run_canada_recipe.py
        """
        chain_data = ExtractionChainBuilder.build_extraction_chain(target_region_id)
        
        # Build recipe steps
        recipe_steps = []
        
        for step in chain_data['steps']:
            if step['action'] == 'extract':
                recipe_steps.append({
                    'type': 'extraction',
                    'source_pbf_id': step['source_pbf_id'],
                    'polygon_file': step['polygon_file'],
                    'output_name': step['target_region'],
                    'strategy': 'complete_ways',  # For history files
                    'cpu_core_id': None  # Auto-assign
                })
        
        return {
            'recipe_name': f"extract_{chain_data['target_region']['name']}",
            'target_region': chain_data['target_region'],
            'chain': chain_data['chain'],
            'steps': recipe_steps,
            'resources': chain_data['resources'],
            'metadata': {
                'created_by': 'extraction_chain_builder',
                'can_execute': chain_data['resources']['can_execute'],
                'requires_planet': True
            }
        }
    
    @staticmethod
    def validate_chain(target_region_id: str) -> Dict:
        """
        Validate if extraction chain can be executed.
        
        Returns validation result with any blocking issues.
        """
        try:
            chain_data = ExtractionChainBuilder.build_extraction_chain(target_region_id)
            
            errors = []
            warnings = []
            
            # Check for errors in steps
            for step in chain_data['steps']:
                if step['action'] == 'error':
                    errors.append({
                        'step': step['step_number'],
                        'region': step['target_region'],
                        'reason': step['reason']
                    })
            
            # Check if planet file exists
            planet_pbf = PbfFile.objects.filter(
                pbf_file_type=PbfFile.PbfType.PLANET,
                status=PbfFile.PbfStatus.COMPLETED
            ).first()
            
            if not planet_pbf:
                errors.append({
                    'step': 0,
                    'region': 'Planet',
                    'reason': 'No planet PBF file registered'
                })
            
            # Check for warnings
            if chain_data['resources']['total_time_hours'] > 2:
                warnings.append({
                    'type': 'long_duration',
                    'message': f"Extraction will take approximately {chain_data['resources']['total_time_hours']} hours"
                })
            
            return {
                'valid': len(errors) == 0,
                'errors': errors,
                'warnings': warnings,
                'can_execute': len(errors) == 0,
                'estimated_time_hours': chain_data['resources']['total_time_hours']
            }
            
        except Exception as e:
            return {
                'valid': False,
                'errors': [{'step': 0, 'region': 'Unknown', 'reason': str(e)}],
                'warnings': [],
                'can_execute': False
            }
