import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
from django.conf import settings
from api.models import PbfFile
from core.services.snapshot.osmium_facade import OsmiumFacade

logger = logging.getLogger(__name__)

class TemporalExtractService:
    """
    Service for generating temporal extracts (yearly/monthly) from region extracts.
    This is the preprocessing layer.
    """
    
    def __init__(self):
        self.osmium = OsmiumFacade()
    
    def generate_temporal_extracts(self, config: Dict) -> Dict:
        """
        Generate temporal extracts based on granularity.
        
        Args:
            config: {
                'source_pbf_id': UUID of source PBF (region or yearly extract),
                'granularity': 'yearly' or 'monthly',
                'start_year': int,
                'end_year': int,
                'output_directory': str,
                'parallel': bool (optional),
                'cpu_cores': int (optional)
            }
        
        Returns:
            {
                'success': bool,
                'extracts_created': int,
                'extract_ids': List[str],
                'total_size_mb': float,
                'processing_time_seconds': float
            }
        """
        try:
            source_pbf = PbfFile.objects.get(id=config['source_pbf_id'])
            granularity = config['granularity']
            start_year = config['start_year']
            end_year = config['end_year']
            
            if granularity == 'yearly':
                return self._generate_yearly_extracts(source_pbf, start_year, end_year, config)
            elif granularity == 'monthly':
                return self._generate_monthly_extracts(source_pbf, start_year, end_year, config)
            else:
                return {
                    'success': False,
                    'error': f'Invalid granularity: {granularity}'
                }
                
        except Exception as e:
            logger.error(f"Error generating temporal extracts: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def _generate_yearly_extracts(self, source_pbf: PbfFile, start_year: int, end_year: int, config: Dict) -> Dict:
        """Generate yearly extracts from region extract."""
        extract_ids = []
        total_size = 0
        start_time = datetime.now()
        
        pbf_path = Path(source_pbf.path)
        
        for year in range(start_year, end_year + 1):
            min_timestamp = f"{year}-01-01T00:00:00Z"
            max_timestamp = f"{year}-12-31T23:59:59Z"
            
            output_path = os.path.join(
                pbf_path.parent,
                'yearly',
                f"{year}.pbf"
            )
            
            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Run osmium time-filter
            # Note: osmium_facade needs to be checked for correct method signature
            result = self.osmium.time_filter(
                source_pbf.path,
                min_timestamp,
                output_path,
                max_timestamp
            )
            
            if result.get('success', False):
                # Create or update PbfFile record
                yearly_extract, created = PbfFile.objects.get_or_create(
                    path=output_path,
                    defaults={
                        'pbf_file_type': 'REGION',
                        'extraction_level': 'REGION_YEARLY',
                        'parent_pbf': source_pbf,
                        'min_timestamp': min_timestamp,
                        'max_timestamp': max_timestamp,
                        'has_history': True
                    }
                )
                
                # Update existing record if not created
                if not created:
                    yearly_extract.min_timestamp = min_timestamp
                    yearly_extract.max_timestamp = max_timestamp
                    yearly_extract.has_history = True
                    yearly_extract.save()
                
                extract_ids.append(str(yearly_extract.id))
                total_size += os.path.getsize(output_path) / (1024 * 1024)  # MB
                
                logger.info(f"Created yearly extract: {output_path}")
        
        processing_time = (datetime.now() - start_time).total_seconds()
        
        # Mark yearly extracts as complete on source PBF
        if len(extract_ids) > 0:
            from django.utils import timezone
            source_pbf.yearly_extracts_generated = True
            source_pbf.yearly_extracts_completed_at = timezone.now()
            source_pbf.yearly_extracts_count = len(extract_ids)
            source_pbf.yearly_extracts_year_range = f"{start_year}-{end_year}"
            source_pbf.save()
            logger.info(f"Marked yearly extracts complete for {source_pbf.path}: {len(extract_ids)} extracts ({start_year}-{end_year})")
        
        return {
            'success': True,
            'extracts_created': len(extract_ids),
            'extract_ids': extract_ids,
            'total_size_mb': round(total_size, 2),
            'processing_time_seconds': round(processing_time, 2)
        }
    
    def _generate_monthly_extracts(self, source_pbf: PbfFile, start_year: int, end_year: int, config: Dict) -> Dict:
        """
        Generate monthly extracts from yearly extract.
        
        IMPORTANT: Monthly extracts can ONLY be created from yearly extracts.
        This enforces the hierarchical constraint: Region → Yearly → Monthly
        """
        # Validate that source is a yearly extract
        if source_pbf.extraction_level != 'REGION_YEARLY':
            return {
                'success': False,
                'error': f'Monthly extracts can only be created from yearly extracts. '
                        f'Source extraction level is {source_pbf.extraction_level}. '
                        f'Please first create a yearly extract, then generate monthly extracts from it.'
            }
        
        extract_ids = []
        total_size = 0
        start_time = datetime.now()
        
        if source_pbf.parent_pbf:
            pbf_path = Path(source_pbf.parent_pbf.path)
        else:
            pbf_path = Path(source_pbf.path)
        
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                # Calculate last day of month
                if month == 12:
                    next_month = 1
                    next_year = year + 1
                else:
                    next_month = month + 1
                    next_year = year
                
                last_day = (datetime(next_year, next_month, 1) - timedelta(days=1)).day
                
                min_timestamp = f"{year}-{month:02d}-01T00:00:00Z"
                max_timestamp = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"
                
                output_path = os.path.join(
                    pbf_path.parent,
                    'monthly',
                    f"{year}_{month:02d}.pbf"
                )
                
                # Ensure output directory exists
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                # Run osmium time-filter
                result = self.osmium.time_filter(
                    source_pbf.path,
                    min_timestamp,
                    output_path,
                    max_timestamp
                )
                
                if result.get('success', False):
                    # Create or update PbfFile record
                    monthly_extract, created = PbfFile.objects.get_or_create(
                        path=output_path,
                        defaults={
                            'pbf_file_type': 'REGION',
                            'extraction_level': 'REGION_MONTHLY',
                            'parent_pbf': source_pbf,
                            'min_timestamp': min_timestamp,
                            'max_timestamp': max_timestamp,
                            'has_history': True
                        }
                    )
                    
                    # Update existing record if not created
                    if not created:
                        monthly_extract.min_timestamp = min_timestamp
                        monthly_extract.max_timestamp = max_timestamp
                        monthly_extract.has_history = True
                        monthly_extract.save()
                    
                    extract_ids.append(str(monthly_extract.id))
                    total_size += os.path.getsize(output_path) / (1024 * 1024)  # MB
                    
                    logger.info(f"Created monthly extract: {output_path}")
        
        processing_time = (datetime.now() - start_time).total_seconds()
        
        # Mark monthly extracts as complete on source PBF (yearly extract)
        if len(extract_ids) > 0:
            from django.utils import timezone
            source_pbf.monthly_extracts_generated = True
            source_pbf.monthly_extracts_completed_at = timezone.now()
            source_pbf.monthly_extracts_count = len(extract_ids)
            source_pbf.monthly_extracts_year_range = f"{start_year}-{end_year}"
            source_pbf.save()
            logger.info(f"Marked monthly extracts complete for {source_pbf.path}: {len(extract_ids)} extracts ({start_year}-{end_year})")
            
            # Also update the parent PBF (original region extract) if it exists
            if source_pbf.parent_pbf:
                parent_pbf = source_pbf.parent_pbf
                # Count all monthly extracts across all yearly extracts for this parent
                total_monthly_count = PbfFile.objects.filter(
                    parent_pbf__parent_pbf=parent_pbf,
                    extraction_level='REGION_MONTHLY'
                ).count()
                
                # Get the year range from all yearly extracts
                yearly_extracts = PbfFile.objects.filter(
                    parent_pbf=parent_pbf,
                    extraction_level='REGION_YEARLY'
                ).order_by('min_timestamp')
                
                if yearly_extracts.exists():
                    min_year = yearly_extracts.first().min_timestamp.year if yearly_extracts.first().min_timestamp else start_year
                    max_year = yearly_extracts.last().max_timestamp.year if yearly_extracts.last().max_timestamp else end_year
                    
                    parent_pbf.monthly_extracts_generated = True
                    parent_pbf.monthly_extracts_completed_at = timezone.now()
                    parent_pbf.monthly_extracts_count = total_monthly_count
                    parent_pbf.monthly_extracts_year_range = f"{min_year}-{max_year}"
                    parent_pbf.save()
                    logger.info(f"Updated parent PBF {parent_pbf.path}: {total_monthly_count} total monthly extracts ({min_year}-{max_year})")
        
        return {
            'success': True,
            'extracts_created': len(extract_ids),
            'extract_ids': extract_ids,
            'total_size_mb': round(total_size, 2),
            'processing_time_seconds': round(processing_time, 2)
        }
