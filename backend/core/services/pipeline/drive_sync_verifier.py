"""
Google Drive Sync Verification Service

Verifies files are successfully synced to local Google Drive folder.
Uses filesystem checks (no API calls needed).
"""

import os
import time
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class DriveSyncVerifier:
    """
    Verify files are successfully synced to Google Drive.
    Uses local filesystem checks (no API calls).
    """
    
    def __init__(self, sync_path):
        """
        Initialize verifier with Google Drive sync path.
        
        Args:
            sync_path: Path to local Google Drive sync folder
        """
        self.sync_path = Path(sync_path)
        
        if not self.sync_path.exists():
            logger.warning(f"Google Drive sync folder not found: {sync_path}")
    
    def verify_sync(self, file_path, timeout=60):
        """
        Verify file exists and is synced to Google Drive.
        
        Args:
            file_path: Path to file in sync folder
            timeout: Max seconds to wait for sync
        
        Returns:
            bool: True if synced, False otherwise
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return False
        
        # Wait for file to stabilize (Google Drive Desktop syncs in background)
        initial_size = file_path.stat().st_size
        time.sleep(2)
        
        # Check if file size is stable (sync complete)
        stable_checks = 0
        for _ in range(timeout // 2):
            current_size = file_path.stat().st_size
            
            if current_size == initial_size:
                stable_checks += 1
                if stable_checks >= 3:  # 3 consecutive stable checks
                    logger.info(f"File synced: {file_path}")
                    return True
            else:
                stable_checks = 0
                initial_size = current_size
            
            time.sleep(2)
        
        logger.warning(f"File may not be fully synced: {file_path}")
        return False
    
    def get_sync_status(self, folder_path):
        """
        Get sync status for a folder.
        
        Args:
            folder_path: Path to folder in sync directory
        
        Returns:
            dict: Sync status information
        """
        folder_path = Path(folder_path)
        
        if not folder_path.exists():
            return {'error': 'Folder not found'}
        
        files = list(folder_path.rglob('*.pbf'))
        total_size = sum(f.stat().st_size for f in files) / (1024 * 1024)  # MB
        
        return {
            'total_files': len(files),
            'total_size_mb': round(total_size, 2),
            'synced': True,  # Assume synced if files exist
            'files': [str(f) for f in files]
        }
    
    def verify_multiple_files(self, file_paths, timeout=60):
        """
        Verify multiple files are synced.
        
        Args:
            file_paths: List of file paths to verify
            timeout: Max seconds to wait per file
        
        Returns:
            dict: Verification results
        """
        results = {
            'total_files': len(file_paths),
            'synced_files': 0,
            'failed_files': 0,
            'details': []
        }
        
        for file_path in file_paths:
            synced = self.verify_sync(file_path, timeout)
            
            if synced:
                results['synced_files'] += 1
            else:
                results['failed_files'] += 1
            
            results['details'].append({
                'file': str(file_path),
                'synced': synced
            })
        
        return results
