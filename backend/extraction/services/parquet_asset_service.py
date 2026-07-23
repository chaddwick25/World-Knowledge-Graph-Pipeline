"""
Parquet Asset Service

Converts TSV graph assets to Parquet format for efficient storage and querying.
Follows the same directory structure as TSV assets but with .parquet extension.

Note: If pyarrow is not available, falls back to CSV format (.csv.gz)

Author: EDA Vector Search Toolkit
"""
import logging
from pathlib import Path
from typing import Dict, List
import time
from extraction.services.regional_path_service import normalize_country_slug

logger = logging.getLogger(__name__)

# Try to import pandas and parquet support
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    pd = None

# Check for pyarrow (Parquet support)
try:
    import pyarrow
    PYARROW_AVAILABLE = True
except ImportError:
    PYARROW_AVAILABLE = False
    logger.info("PyArrow not available, will use CSV format instead of Parquet")


class ParquetAssetService:
    """
    Service for converting TSV graph assets to Parquet or CSV format.
    
    Converts:
    - nodes.tsv → nodes.parquet (or nodes.csv.gz if pyarrow unavailable)
    - edges.tsv → edges.parquet (or edges.csv.gz)
    - tags.tsv → tags.parquet (or tags.csv.gz)
    
    Maintains same directory structure as TSV assets.
    """
    
    def __init__(self, format='auto'):
        """
        Args:
            format: 'parquet', 'csv', or 'auto' (auto-detect based on pyarrow availability)
        """
        if format == 'auto':
            self.format = 'parquet' if PYARROW_AVAILABLE else 'csv'
        else:
            self.format = format
        self.compression = 'snappy' if self.format == 'parquet' else 'gzip'
    
    def convert_tsv_file(self, tsv_path: Path, output_path: Path) -> Dict:
        """
        Convert a single TSV file to Parquet or CSV format.
        
        Args:
            tsv_path: Path to input TSV file
            output_path: Path to output file (.parquet or .csv.gz)
        
        Returns:
            {
                'success': bool,
                'tsv_size_mb': float,
                'output_size_mb': float,
                'compression_ratio': float,
                'rows': int,
                'duration': float,
                'format': str
            }
        """
        if not PANDAS_AVAILABLE:
            return {
                'success': False,
                'error': 'pandas not available'
            }
        
        try:
            start_time = time.time()
            
            # Read TSV file
            df = pd.read_csv(tsv_path, sep='\t')
            
            # Get TSV size
            tsv_size = tsv_path.stat().st_size / (1024 * 1024)  # MB
            
            # Write output file based on format
            if self.format == 'parquet' and PYARROW_AVAILABLE:
                df.to_parquet(
                    output_path,
                    engine='pyarrow',
                    compression=self.compression,
                    index=False
                )
                output_format = 'parquet'
            else:
                # Use CSV with gzip compression as fallback
                csv_path = str(output_path).replace('.parquet', '.csv.gz')
                df.to_csv(
                    csv_path,
                    compression='gzip',
                    index=False
                )
                output_path = Path(csv_path)
                output_format = 'csv.gz'
            
            # Get output size
            output_size = output_path.stat().st_size / (1024 * 1024)  # MB
            
            duration = time.time() - start_time
            compression_ratio = tsv_size / output_size if output_size > 0 else 0
            
            logger.info(f"Converted {tsv_path.name}: {tsv_size:.2f}MB → {output_size:.2f}MB ({compression_ratio:.1f}x compression) [{output_format}]")
            
            return {
                'success': True,
                'tsv_size_mb': tsv_size,
                'output_size_mb': output_size,
                'compression_ratio': compression_ratio,
                'rows': len(df),
                'duration': duration,
                'format': output_format
            }
            
        except Exception as e:
            logger.error(f"Failed to convert {tsv_path}: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def convert_asset_bundle(self, tsv_dir: Path, parquet_dir: Path = None) -> Dict:
        """
        Convert all TSV files in an asset bundle directory to Parquet.
        
        Args:
            tsv_dir: Directory containing TSV files (nodes.tsv, edges.tsv, tags.tsv)
            parquet_dir: Output directory for Parquet files (defaults to same as tsv_dir)
        
        Returns:
            {
                'success': bool,
                'files_converted': int,
                'total_tsv_size_mb': float,
                'total_parquet_size_mb': float,
                'avg_compression_ratio': float,
                'duration': float,
                'files': [...]
            }
        """
        if parquet_dir is None:
            parquet_dir = tsv_dir
        
        parquet_dir = Path(parquet_dir)
        parquet_dir.mkdir(parents=True, exist_ok=True)
        
        start_time = time.time()
        results = []
        
        # Convert each TSV file
        for tsv_file in ['nodes.tsv', 'edges.tsv', 'tags.tsv']:
            tsv_path = tsv_dir / tsv_file
            
            if not tsv_path.exists():
                logger.warning(f"TSV file not found: {tsv_path}")
                continue
            
            parquet_file = tsv_file.replace('.tsv', '.parquet')
            parquet_path = parquet_dir / parquet_file
            
            result = self.convert_tsv_file(tsv_path, parquet_path)
            result['file'] = tsv_file
            results.append(result)
        
        # Calculate summary statistics
        successful = [r for r in results if r.get('success')]
        total_tsv_size = sum(r.get('tsv_size_mb', 0) for r in successful)
        total_output_size = sum(r.get('output_size_mb', 0) for r in successful)
        avg_compression = sum(r.get('compression_ratio', 0) for r in successful) / len(successful) if successful else 0
        
        duration = time.time() - start_time
        
        return {
            'success': len(successful) > 0,
            'files_converted': len(successful),
            'total_tsv_size_mb': total_tsv_size,
            'total_output_size_mb': total_output_size,
            'avg_compression_ratio': avg_compression,
            'duration': duration,
            'files': results
        }
    
    def batch_convert_monthly_assets(self, base_dir: Path, country_name: str, year_filter: int = None) -> Dict:
        """
        Convert all monthly TSV assets to Parquet for a country.
        
        Args:
            base_dir: Base directory containing graph assets (e.g., /data/graph-assets/)
            country_name: Country name (e.g., 'andorra')
            year_filter: Optional year to filter (e.g., 2025 for testing)
        
        Returns:
            {
                'success': bool,
                'months_processed': int,
                'total_files_converted': int,
                'total_tsv_size_mb': float,
                'total_parquet_size_mb': float,
                'avg_compression_ratio': float,
                'duration': float,
                'monthly_results': [...]
            }
        """
        country_dir = base_dir / normalize_country_slug(country_name)
        
        if not country_dir.exists():
            return {
                'success': False,
                'error': f'Country directory not found: {country_dir}'
            }
        
        start_time = time.time()
        monthly_results = []
        
        # Find all monthly directories
        month_dirs = sorted([d for d in country_dir.iterdir() if d.is_dir()])
        
        # Filter by year if specified
        if year_filter:
            month_dirs = [d for d in month_dirs if d.name.startswith(str(year_filter))]
        
        logger.info(f"Converting {len(month_dirs)} monthly asset bundles for {country_name}")
        if year_filter:
            logger.info(f"  Filtered to year: {year_filter}")
        
        for month_dir in month_dirs:
            logger.info(f"Processing {month_dir.name}...")
            
            result = self.convert_asset_bundle(month_dir)
            result['month'] = month_dir.name
            monthly_results.append(result)
        
        # Calculate summary statistics
        successful = [r for r in monthly_results if r.get('success')]
        total_files = sum(r.get('files_converted', 0) for r in successful)
        total_tsv_size = sum(r.get('total_tsv_size_mb', 0) for r in successful)
        total_parquet_size = sum(r.get('total_parquet_size_mb', 0) for r in successful)
        avg_compression = sum(r.get('avg_compression_ratio', 0) for r in successful) / len(successful) if successful else 0
        
        duration = time.time() - start_time
        
        logger.info(f"Batch conversion completed in {duration:.1f}s")
        logger.info(f"  Months: {len(successful)}/{len(monthly_results)}")
        logger.info(f"  Files: {total_files}")
        logger.info(f"  TSV: {total_tsv_size:.1f}MB → Parquet: {total_parquet_size:.1f}MB")
        logger.info(f"  Avg compression: {avg_compression:.1f}x")
        
        return {
            'success': len(successful) > 0,
            'months_processed': len(successful),
            'total_files_converted': total_files,
            'total_tsv_size_mb': total_tsv_size,
            'total_parquet_size_mb': total_parquet_size,
            'avg_compression_ratio': avg_compression,
            'duration': duration,
            'monthly_results': monthly_results
        }
