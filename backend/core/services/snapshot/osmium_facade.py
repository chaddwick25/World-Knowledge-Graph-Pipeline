import subprocess
import os
from datetime import datetime
from django.conf import settings

class OsmiumFacade:
    OSMIUM_EXECUTABLE = str(getattr(settings, 'OSMIUM_EXECUTABLE', 'osmium'))

    def tags_count(self, input_file):
        if not os.path.exists(input_file):
            return {"error": "File does not exist", "exit_code": 1}

        cmd = [self.OSMIUM_EXECUTABLE, "tags-count", input_file]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return {"error": result.stderr, "exit_code": result.returncode}

        import re
        tag_distribution = {}
        # Regex to handle optional quotes around the tag and capture the count
        # It matches either a quoted string or a non-space string for the tag
        line_regex = re.compile(r'^(".*?"|\S+)\s+(\d+)$')

        for line in result.stdout.strip().split('\n'):
            match = line_regex.match(line)
            if match:
                tag, count = match.groups()
                # Remove quotes from the tag if they exist
                if tag.startswith('"') and tag.endswith('"'):
                    tag = tag[1:-1]
                tag_distribution[tag] = int(count)

        return {
            "exit_code": 0,
            "tag_distribution": tag_distribution,
            "unique_tag_count": len(tag_distribution)
        }

    def get_bbox(self, input_file: str):
        """
        Fast bounding box extraction via C++ binary.
        Returns [minlon, minlat, maxlon, maxlat] or None on failure.
        Uses 'osmium fileinfo -g data.bbox' which reads the PBF header metadata
        first, and only does a full scan if the header lacks bbox info.
        """
        if not os.path.exists(input_file):
            return None
        cmd = [self.OSMIUM_EXECUTABLE, "fileinfo", "-g", "data.bbox", input_file]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        # Output format: "minlon,minlat,maxlon,maxlat"
        try:
            parts = result.stdout.strip().split(',')
            return [float(p) for p in parts]  # [minlon, minlat, maxlon, maxlat]
        except (ValueError, IndexError):
            return None

    def file_info(self, input_file):

        if not os.path.exists(input_file):
            return {"error": "File does not exist", "exit_code": 1}

        cmd = [self.OSMIUM_EXECUTABLE, "fileinfo", "--json", input_file]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return {"error": result.stderr, "exit_code": result.returncode}

        import json
        info = json.loads(result.stdout)
        return {
            "exit_code": result.returncode,
            "info": info,
            "raw_output": result.stdout
        }

    def time_filter(self, input_file, timestamp, output_file, end_timestamp=None):
        cmd = [
            self.OSMIUM_EXECUTABLE, "time-filter", "--progress",
            input_file
        ]
        
        # Add timestamp(s)
        cmd.append(str(timestamp))
        if end_timestamp:
            cmd.append(str(end_timestamp))
            
        cmd.extend([
            "--output", output_file,
            "--overwrite"
        ])
        
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=None, text=True)

        if result.returncode != 0:
            return {"success": False, "error": "Osmium failed (see terminal for details)", "exit_code": result.returncode}

        return {"success": True, "exit_code": 0, "output_file": output_file}

    def cat(self, input_file, output_format='json'):
        """Run osmium cat command to output file contents in specified format."""
        cmd = [self.OSMIUM_EXECUTABLE, "cat", "--output-format", output_format, input_file]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return {"error": result.stderr, "exit_code": result.returncode}
        
        return {
            "exit_code": result.returncode,
            "output": result.stdout
        }

    def tags_filter(self, input_file, output_file, expressions, invert_match=False):
        """
        Filter PBF file by tag expressions using osmium tags-filter.
        
        Args:
            input_file: Input PBF path
            output_file: Output PBF path
            expressions: List of tag expressions (e.g., ['amenity=restaurant', 'highway'])
            invert_match: If True, exclude matching entities instead of including
        
        Returns:
            {'success': bool, 'output_file': str, 'error': str}
        
        Example:
            osmium.tags_filter(
                'input.osm.pbf',
                'output.osm.pbf',
                ['amenity=restaurant', 'amenity=cafe', 'cuisine']
            )
        """
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            cmd = [self.OSMIUM_EXECUTABLE, 'tags-filter', '--progress', input_file]
            
            # Add expressions
            cmd.extend(expressions)
            
            # Add output
            cmd.extend(['-o', output_file])
            
            # Invert match if requested
            if invert_match:
                cmd.append('--invert-match')
            
            # Overwrite output file if exists
            cmd.append('--overwrite')
            
            logger.info(f"Running osmium tags-filter: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                timeout=3600  # 1 hour timeout
            )
            
            if result.returncode != 0:
                error_msg = "Osmium failed (see terminal for details)"
                logger.error(f"osmium tags-filter failed: {error_msg}")
                return {
                    'success': False,
                    'error': error_msg,
                    'exit_code': result.returncode
                }
            
            logger.info(f"Successfully filtered PBF to: {output_file}")
            
            return {
                'success': True,
                'output_file': output_file,
                'exit_code': 0
            }
            
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'Command timeout (>1 hour)'}
        except Exception as e:
            logger.error(f"Error in tags_filter: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
