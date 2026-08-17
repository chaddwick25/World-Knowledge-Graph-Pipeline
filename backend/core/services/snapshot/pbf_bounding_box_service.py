import os
import osmium
import logging
from pathlib import Path
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


class BoundingBoxHandler(osmium.SimpleHandler):
    """
    Fallback Python osmium handler to calculate the bounding box of all
    nodes and ways in a PBF. Used only when the fast CLI path fails.
    """
    def __init__(self):
        super().__init__()
        # [minlon, minlat, maxlon, maxlat]
        self.bounds = [180.0, 90.0, -180.0, -90.0]
        self.has_data = False

    def node(self, n):
        self.has_data = True
        self.bounds[0] = min(self.bounds[0], n.location.lon)
        self.bounds[1] = min(self.bounds[1], n.location.lat)
        self.bounds[2] = max(self.bounds[2], n.location.lon)
        self.bounds[3] = max(self.bounds[3], n.location.lat)

    def way(self, w):
        self.has_data = True
        for node in w.nodes:
            if node.location.valid():
                self.bounds[0] = min(self.bounds[0], node.location.lon)
                self.bounds[1] = min(self.bounds[1], node.location.lat)
                self.bounds[2] = max(self.bounds[2], node.location.lon)
                self.bounds[3] = max(self.bounds[3], node.location.lat)


class PbfBoundingBoxService:
    """
    Calculates geographic bounds of a PBF file and generates .poly files.

    Primary path  : osmium fileinfo -g data.bbox  (C++ binary, reads header — very fast)
    Fallback path : Python BoundingBoxHandler      (full node scan — always works)
    """

    def __init__(self):
        from .osmium_facade import OsmiumFacade
        self._osmium = OsmiumFacade()

    def get_pbf_bounds(self, pbf_file: str) -> Tuple[float, float, float, float]:
        """
        Return (minlon, minlat, maxlon, maxlat) for a PBF file.
        Tries the fast CLI path first; falls back to the Python handler.
        """
        if not Path(pbf_file).exists():
            raise FileNotFoundError(f"PBF file not found: {pbf_file}")

        # --- Fast path: C++ CLI (reads PBF header — typically sub-second) ---
        bbox = self._osmium.get_bbox(pbf_file)
        if bbox and len(bbox) == 4:
            logger.debug(f"Fast bbox for {pbf_file}: {bbox}")
            return tuple(bbox)

        # --- Fallback: Python handler (full node scan) ---
        logger.debug(f"CLI bbox failed for {pbf_file}, falling back to Python handler")
        handler = BoundingBoxHandler()
        reader = osmium.io.Reader(
            str(pbf_file),
            osmium.osm.osm_entity_bits.NODE | osmium.osm.osm_entity_bits.WAY,
        )
        osmium.apply(reader, handler)
        reader.close()

        if not handler.has_data:
            return (0.0, 0.0, 0.0, 0.0)

        return tuple(handler.bounds)

    def generate_poly_from_pbf(self, pbf_file: str, output_poly: str) -> Optional[Tuple]:
        """
        Extract bounding box from a PBF snapshot and write it as a polyfile.
        Returns the bounds tuple, or None if the file is empty / does not exist.
        """
        try:
            minlon, minlat, maxlon, maxlat = self.get_pbf_bounds(pbf_file)
        except FileNotFoundError:
            logger.warning(f"Skipping poly generation — PBF not found: {pbf_file}")
            return None

        # Guard against empty/invalid files
        if minlon == maxlon == minlat == maxlat == 0.0:
            logger.warning(f"Skipping poly generation — empty bounds for: {pbf_file}")
            return None

        # Ensure target directory exists
        Path(output_poly).parent.mkdir(parents=True, exist_ok=True)

        poly_name = Path(output_poly).stem

        with open(output_poly, 'w') as f:
            f.write(f"{poly_name}\n")           # Header (poly name)
            f.write("1\n")                       # Single ring
            f.write(f"  {minlon:.6f}  {minlat:.6f}\n")
            f.write(f"  {maxlon:.6f}  {minlat:.6f}\n")
            f.write(f"  {maxlon:.6f}  {maxlat:.6f}\n")
            f.write(f"  {minlon:.6f}  {maxlat:.6f}\n")
            f.write(f"  {minlon:.6f}  {minlat:.6f}\n")  # Close the ring
            f.write("END\n")
            f.write("END\n")

        logger.info(
            f"Generated polyfile: {output_poly} "
            f"bounds=({minlon:.4f},{minlat:.4f},{maxlon:.4f},{maxlat:.4f})"
        )
        return (minlon, minlat, maxlon, maxlat)

    def generate_high_res_poly(self, pbf_file: str, relation_id: int, output_poly: str) -> bool:
        """
        Generate a high-resolution .poly file from a specific relation ID within a PBF.
        This provides a precise territorial boundary instead of a rectangular bbox.
        """
        import subprocess
        import json
        import tempfile
        from django.conf import settings

        # Validate relation_id
        if not relation_id or relation_id == '':
            logger.error(f"Phase 3: Invalid relation_id: '{relation_id}'. Cannot generate poly.")
            return False

        try:
            # 1. Extract relation into a temp PBF
            with tempfile.NamedTemporaryFile(suffix='.pbf', delete=False) as tmp:
                tmp_pbf = tmp.name

            getid_cmd = [
                settings.OSMIUM_BINARY_PATH, 'getid',
                '--with-history',
                '--add-referenced',
                pbf_file, f"r{relation_id}",
                '-o', tmp_pbf,
                '--overwrite'
            ]

            logger.info(f"Phase 3: Extracting r{relation_id} from {pbf_file}...")
            try:
                subprocess.run(getid_cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Osmium getid FAILED for r{relation_id}: {e.stderr} (stdout: {e.stdout})")
                return False
            
            # 2. Export to GeoJSON
            export_cmd = [
                settings.OSMIUM_BINARY_PATH, 'export',
                '-f', 'geojson',
                tmp_pbf
            ]
            try:
                res = subprocess.run(export_cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Osmium export FAILED for r{relation_id}: {e.stderr} (stdout: {e.stdout})")
                return False
                
            geojson = json.loads(res.stdout)
            
            # 3. Parse GeoJSON and write .poly
            features = geojson.get('features', [])
            if not features:
                logger.warning(f"No features found for r{relation_id} in {pbf_file}")
                return False
                
            # Find the actual boundary feature (Polygon or MultiPolygon)
            boundary_feature = None
            for f in features:
                geom_type = f.get('geometry', {}).get('type')
                if geom_type in ['Polygon', 'MultiPolygon']:
                    boundary_feature = f
                    # If we find the country level (admin_level=2), stop immediately
                    if f.get('properties', {}).get('admin_level') == '2':
                        break
            
            if not boundary_feature:
                logger.warning(f"No polygon/multipolygon features found for r{relation_id}")
                return False

            geom = boundary_feature.get('geometry', {})
            coords_list = []
            
            if geom.get('type') == 'Polygon':
                coords_list = geom.get('coordinates', [])
            elif geom.get('type') == 'MultiPolygon':
                # Use all polygons in the multipolygon
                # GeoJSON MultiPolygon is list of polygons, each polygon is list of rings
                all_rings = []
                for poly in geom.get('coordinates', []):
                    for ring in poly:
                        all_rings.append(ring)
                coords_list = all_rings
            
            if not coords_list:
                logger.error(f"Empty coordinates for r{relation_id} boundary")
                return False
                
            Path(output_poly).parent.mkdir(parents=True, exist_ok=True)
            poly_name = Path(output_poly).stem
            
            with open(output_poly, 'w') as f:
                f.write(f"{poly_name}\n")
                for i, ring in enumerate(coords_list):
                    f.write(f"{i + 1}\n")
                    for lon, lat in ring:
                        f.write(f"  {lon:.6f}  {lat:.6f}\n")
                    f.write("END\n")
                f.write("END\n")
            
            # Save GeoJSON as a 'simple file' backup/verification
            geojson_path = Path(output_poly).with_suffix('.json')
            with open(geojson_path, 'w') as f:
                json.dump(boundary_feature, f, indent=2)
                
            logger.info(f"Generated high-res polyfile and geojson for r{relation_id} from {Path(pbf_file).name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to generate high-res poly for r{relation_id}: {e}")
            return False
        finally:
            if 'tmp_pbf' in locals() and os.path.exists(tmp_pbf):
                os.unlink(tmp_pbf)


pbf_bounding_box_service = PbfBoundingBoxService()
