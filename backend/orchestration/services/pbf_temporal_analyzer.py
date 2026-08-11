import os
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from django.utils import timezone
from django.conf import settings
from api.models import PbfFile, OsmiumDatasetMetrics, ProcessingSession
import logging
import osmium

logger = logging.getLogger(__name__)

# TODO: remove this implementation - set a start date (based on the OSM pre-trained embeddings date) and the enddate with be based on the planet file or a overrideable end date set at the initialization of the project/pipeline
class TimestampSamplingHandler(osmium.SimpleHandler):
    """Handler to collect timestamps from OSM nodes for temporal range analysis"""
    
    def __init__(self, max_samples=1000):
        super().__init__()
        self.timestamps = []
        self.max_samples = max_samples
        self.sample_count = 0
    
    def node(self, n):
        if self.sample_count >= self.max_samples:
            return
        if n.timestamp:
            self.timestamps.append(n.timestamp)
            self.sample_count += 1

# TODO: Remove this as well
class PbfTemporalAnalyzer:
    """
    Service to analyze temporal coverage of PBF files and populate database metrics.
    Uses osmium fileinfo and show commands to extract temporal metadata.
    """
    
    def __init__(self):
        self.osmium_path = getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium')
    
    def analyze_pbf_temporal_coverage(self, pbf_file: PbfFile, force_refresh: bool = False, parent_session: ProcessingSession = None):
        """
        Analyze temporal coverage of a PBF file and update/create metrics record.
        
        Args:
            pbf_file: PbfFile instance to analyze
            force_refresh: Whether to recompute even if metrics exist
            
        Returns:
            dict: Temporal coverage information
        """
        try:
            # Check if metrics already exist
            existing_metrics = OsmiumDatasetMetrics.objects.filter(pbf_file=pbf_file).first()
            
            if existing_metrics and not force_refresh:
                if existing_metrics.temporal_coverage_start and existing_metrics.temporal_coverage_end:
                    return {
                        'start_date': existing_metrics.temporal_coverage_start,
                        'end_date': existing_metrics.temporal_coverage_end,
                        'resolution_days': existing_metrics.temporal_resolution_days,
                        'source': 'existing_metrics'
                    }
            
            # Validate file exists
            if not pbf_file.path or not os.path.exists(pbf_file.path):
                raise ValueError(f"PBF file not found: {pbf_file.path}")
            
            logger.info(f"Analyzing temporal coverage for {pbf_file.path}")
            
            # Get temporal coverage using osmium
            temporal_info = self._extract_temporal_info(pbf_file.path)
            
            if not temporal_info:
                logger.warning(f"Could not extract temporal info from {pbf_file.path}")
                return None
            
            # Update or create metrics record
            # Return the raw temporal info and the existing metrics object (if any)
            return {
                'temporal_info': temporal_info,
                'existing_metrics': existing_metrics
            }
            
        except Exception as e:
            logger.error(f"Error analyzing temporal coverage for {pbf_file.path}: {e}")
            return None
    
    def _extract_temporal_info(self, pbf_path: str):
        """Extract temporal information using osmium commands"""
        try:
            # Method 1: Try osmium fileinfo for basic metadata
            fileinfo_result = self._run_osmium_fileinfo(pbf_path)
            
            # Method 2: For history files, use osmium show to sample timestamps
            if fileinfo_result.get('has_history', False):
                temporal_sample = self._sample_temporal_range(pbf_path)
                if temporal_sample:
                    fileinfo_result.update(temporal_sample)
            
            return fileinfo_result
            
        except Exception as e:
            logger.error(f"Error extracting temporal info from {pbf_path}: {e}")
            return None
    
    def _run_osmium_fileinfo(self, pbf_path: str):
        """Run osmium fileinfo to get basic file metadata"""
        try:
            cmd = [self.osmium_path, 'fileinfo', '-e', pbf_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.error(f"osmium fileinfo failed: {result.stderr}")
                return None
            
            # Parse fileinfo output
            info = {}
            for line in result.stdout.split('\n'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    info[key.strip()] = value.strip()
            
            # Extract relevant temporal information
            temporal_info = {
                'file_size': int(info.get('file_size', 0)),
                'has_history': info.get('with_history', 'no').lower() == 'yes',
                'generator': info.get('generator', ''),
                'total_objects': 0
            }
            
            # Extract object counts
            for obj_type in ['nodes', 'ways', 'relations']:
                count_key = f'num_{obj_type}'
                if count_key in info:
                    count = int(info[count_key])
                    temporal_info[f'total_{obj_type}'] = count
                    temporal_info['total_objects'] += count
            
            # Try to extract timestamp from generator or other metadata
            if 'timestamp' in info:
                try:
                    timestamp = datetime.fromisoformat(info['timestamp'].replace('Z', '+00:00'))
                    temporal_info['file_timestamp'] = timestamp
                except Exception as e:
                    logger.debug(f"Failed to parse timestamp from info: {e}")
            
            return temporal_info
            
        except subprocess.TimeoutExpired:
            logger.error(f"osmium fileinfo timeout for {pbf_path}")
            return None
        except Exception as e:
            logger.error(f"Error running osmium fileinfo: {e}")
            return None
    
    def _sample_temporal_range(self, pbf_path: str, sample_size: int = 1000):
        """
        Sample temporal range from history PBF by examining object timestamps.
        Uses pyosmium to extract a sample of timestamps.
        """
        try:
            handler = TimestampSamplingHandler(max_samples=sample_size)
            handler.apply_file(pbf_path, locations=False, idx='sparse_file_array')
            
            if not handler.timestamps:
                logger.warning(f"No timestamps found in sample from {pbf_path}")
                return None
            
            # Convert osmium timestamps to datetime and sort
            timestamps = sorted([ts.to_datetime() for ts in handler.timestamps])
            start_date = timestamps[0]
            end_date = timestamps[-1]
            
            # Calculate average resolution (time between samples)
            if len(timestamps) > 1:
                total_duration = (end_date - start_date).total_seconds()
                resolution_days = total_duration / (len(timestamps) - 1) / 86400  # Convert to days
            else:
                resolution_days = None
            
            logger.info(f"Temporal range: {start_date} to {end_date} ({len(timestamps)} samples)")
            
            return {
                'start_date': start_date,
                'end_date': end_date,
                'resolution_days': resolution_days,
                'sample_count': len(timestamps)
            }
            
        except Exception as e:
            logger.error(f"Error sampling temporal range: {e}")
            return None
    
    
    def _get_osmium_version(self):
        """Get osmium version for metadata"""
        try:
            cmd = [self.osmium_path, '--version']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return result.stdout.strip().split('\n')[0]
        except Exception as e:
            logger.debug(f"Failed to get osmium version: {e}")
        return 'unknown'
    
    def analyze_all_pbf_files(self, force_refresh: bool = False, pbf_type_filter: str = None):
        """
        Analyze temporal coverage for all PBF files in the database.
        
        Args:
            force_refresh: Whether to recompute existing metrics
            pbf_type_filter: Filter by PBF file type ('HISTORICAL' or 'LATEST')
            
        Returns:
            dict: Summary of analysis results
        """
        try:
            # Get PBF files to analyze
            pbf_files = PbfFile.objects.filter(status=PbfFile.PbfStatus.COMPLETED)
            
            if pbf_type_filter:
                pbf_files = pbf_files.filter(pbf_file_type=pbf_type_filter)
            
            results = {
                'total_files': pbf_files.count(),
                'analyzed': 0,
                'failed': 0,
                'skipped': 0,
                'temporal_ranges': {}
            }
            
            logger.info(f"Starting temporal analysis for {results['total_files']} PBF files")
            
            for pbf_file in pbf_files:
                try:
                    logger.info(f"Analyzing {pbf_file.path}")
                    
                    temporal_info = self.analyze_pbf_temporal_coverage(pbf_file, force_refresh)
                    
                    if temporal_info:
                        results['analyzed'] += 1
                        results['temporal_ranges'][str(pbf_file.id)] = {
                            'path': pbf_file.path,
                            'start_date': temporal_info['start_date'].isoformat() if temporal_info['start_date'] else None,
                            'end_date': temporal_info['end_date'].isoformat() if temporal_info['end_date'] else None,
                            'resolution_days': temporal_info.get('resolution_days'),
                            'source': temporal_info.get('source')
                        }
                    else:
                        results['failed'] += 1
                        
                except Exception as e:
                    results['failed'] += 1
                    logger.error(f"Failed to analyze {pbf_file.path}: {e}")
            
            logger.info(f"Temporal analysis complete: {results['analyzed']} analyzed, {results['failed']} failed")
            return results
            
        except Exception as e:
            logger.error(f"Error in batch temporal analysis: {e}")
            raise
    
    def get_pbf_temporal_summary(self):
        """Get summary of temporal coverage for all PBF files"""
        try:
            metrics = OsmiumDatasetMetrics.objects.filter(
                temporal_coverage_start__isnull=False,
                temporal_coverage_end__isnull=False
            ).select_related('pbf_file')
            
            summary = {
                'total_files_with_temporal_data': metrics.count(),
                'global_start_date': None,
                'global_end_date': None,
                'files_by_type': {},
                'continental_coverage': {}
            }
            
            if metrics.exists():
                # Calculate global temporal range
                summary['global_start_date'] = metrics.order_by('temporal_coverage_start').first().temporal_coverage_start
                summary['global_end_date'] = metrics.order_by('-temporal_coverage_end').first().temporal_coverage_end
                
                # Group by file type
                for metric in metrics:
                    file_type = metric.pbf_file.pbf_file_type
                    if file_type not in summary['files_by_type']:
                        summary['files_by_type'][file_type] = {
                            'count': 0,
                            'start_date': None,
                            'end_date': None
                        }
                    
                    type_info = summary['files_by_type'][file_type]
                    type_info['count'] += 1
                    
                    if not type_info['start_date'] or metric.temporal_coverage_start < type_info['start_date']:
                        type_info['start_date'] = metric.temporal_coverage_start
                    
                    if not type_info['end_date'] or metric.temporal_coverage_end > type_info['end_date']:
                        type_info['end_date'] = metric.temporal_coverage_end
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting temporal summary: {e}")
            return None
