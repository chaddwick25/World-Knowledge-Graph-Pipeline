import osmium
import logging
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

class AssetExtractor(osmium.SimpleHandler):
    """
    Osmium handler that extracts nodes, edges, and tags from a PBF file
    and writes them to Parquet files chunk-by-chunk for downstream processing.
    """
    
    def __init__(self, output_dir: Path):
        osmium.SimpleHandler.__init__(self)
        self.output_dir = Path(output_dir)
        self.batch_size = 500000
        
        # Define schemas
        self.nodes_schema = pa.schema([
            ('osm_id', pa.int64()),
            ('lat', pa.float64()),
            ('lon', pa.float64())
        ])
        
        self.edges_schema = pa.schema([
            ('way_id', pa.int64()),
            ('osm_id_a', pa.int64()),
            ('osm_id_b', pa.int64())
        ])
        
        self.tags_schema = pa.schema([
            ('osm_id', pa.int64()),
            ('key', pa.string()),
            ('value', pa.string())
        ])
        
        # Open Parquet writers
        self.nodes_writer = pq.ParquetWriter(self.output_dir / 'nodes.parquet', self.nodes_schema, compression='snappy')
        self.edges_writer = pq.ParquetWriter(self.output_dir / 'edges.parquet', self.edges_schema, compression='snappy')
        self.tags_writer = pq.ParquetWriter(self.output_dir / 'tags.parquet', self.tags_schema, compression='snappy')
        
        # Initialize buffers
        self._init_node_buffer()
        self._init_edge_buffer()
        self._init_tag_buffer()
        
        # Statistics
        self.node_count = 0
        self.way_count = 0
        self.relation_count = 0
        self.edge_count = 0
        self.tag_count = 0
        
        logger.info(f"AssetExtractor initialized for Parquet. Output directory: {output_dir}")

    def _init_node_buffer(self):
        self.n_ids, self.n_lats, self.n_lons = [], [], []

    def _init_edge_buffer(self):
        self.e_way, self.e_a, self.e_b = [], [], []

    def _init_tag_buffer(self):
        self.t_ids, self.t_keys, self.t_vals = [], [], []

    def _flush_nodes(self):
        if not self.n_ids: return
        table = pa.Table.from_arrays([
            pa.array(self.n_ids), pa.array(self.n_lats), pa.array(self.n_lons)
        ], schema=self.nodes_schema)
        self.nodes_writer.write_table(table)
        self._init_node_buffer()

    def _flush_edges(self):
        if not self.e_a: return
        table = pa.Table.from_arrays([
            pa.array(self.e_way), pa.array(self.e_a), pa.array(self.e_b)
        ], schema=self.edges_schema)
        self.edges_writer.write_table(table)
        self._init_edge_buffer()

    def _flush_tags(self):
        if not self.t_ids: return
        table = pa.Table.from_arrays([
            pa.array(self.t_ids), pa.array(self.t_keys), pa.array(self.t_vals)
        ], schema=self.tags_schema)
        self.tags_writer.write_table(table)
        self._init_tag_buffer()

    def node(self, n):
        try:
            # Check for valid location before touching arrays to prevent desync
            try:
                lat = n.location.lat
                lon = n.location.lon
                self.n_ids.append(n.id)
                self.n_lats.append(lat)
                self.n_lons.append(lon)
                self.node_count += 1
            except osmium.InvalidLocationError:
                # Silently ignore nodes without valid geographic locations
                pass
            
            for tag in n.tags:
                self.t_ids.append(n.id)
                self.t_keys.append(tag.k)
                self.t_vals.append(tag.v)
                self.tag_count += 1
                
            if len(self.n_ids) >= self.batch_size:
                self._flush_nodes()
            if len(self.t_ids) >= self.batch_size:
                self._flush_tags()
                
            if self.node_count % 500000 == 0 and self.node_count > 0:
                logger.info(f"Processed {self.node_count:,} nodes...")
                
        except Exception as e:
            logger.warning(f"Error processing node {n.id}: {e}")

    def way(self, w):
        try:
            # Parse geographical centroid of the way
            coords = []
            for n in w.nodes:
                try:
                    coords.append((n.lon, n.lat))
                except (osmium.InvalidLocationError, AttributeError):
                    pass
            
            if coords:
                lon = sum(c[0] for c in coords) / len(coords)
                lat = sum(c[1] for c in coords) / len(coords)
                # Append to nodes parquet buffer to provide coordinate features for GNN
                self.n_ids.append(w.id)
                self.n_lats.append(lat)
                self.n_lons.append(lon)
                self.node_count += 1
                if len(self.n_ids) >= self.batch_size:
                    self._flush_nodes()

            if len(w.nodes) > 1:
                for i in range(len(w.nodes) - 1):
                    self.e_way.append(w.id)
                    self.e_a.append(w.nodes[i].ref)
                    self.e_b.append(w.nodes[i + 1].ref)
                    self.edge_count += 1
            self.way_count += 1
            
            for tag in w.tags:
                self.t_ids.append(w.id)
                self.t_keys.append(tag.k)
                self.t_vals.append(tag.v)
                self.tag_count += 1

            if len(self.e_a) >= self.batch_size:
                self._flush_edges()
            if len(self.t_ids) >= self.batch_size:
                self._flush_tags()
                
            if self.way_count % 100000 == 0:
                logger.info(f"Processed {self.way_count:,} ways, {self.edge_count:,} edges...")
                
        except Exception as e:
            logger.warning(f"Error processing way {w.id}: {e}")

    def relation(self, r):
        try:
            self.relation_count += 1
            for tag in r.tags:
                self.t_ids.append(r.id)
                self.t_keys.append(tag.k)
                self.t_vals.append(tag.v)
                self.tag_count += 1
                
            if len(self.t_ids) >= self.batch_size:
                self._flush_tags()
                
        except Exception as e:
            logger.warning(f"Error processing relation {r.id}: {e}")

    def close_files(self):
        """Flush remaining buffers and close Parquet writers."""
        self._flush_nodes()
        self._flush_edges()
        self._flush_tags()
        
        self.nodes_writer.close()
        self.edges_writer.close()
        self.tags_writer.close()
        
        logger.info(f"Asset extraction via Parquet complete:")
        logger.info(f"  - Nodes: {self.node_count}")
        logger.info(f"  - Ways: {self.way_count}")
        logger.info(f"  - Relations: {self.relation_count}")
        logger.info(f"  - Edges: {self.edge_count}")
        logger.info(f"  - Tags: {self.tag_count}")

def extract_assets_from_pbf(pbf_path: str, output_dir: Path) -> dict:
    """
    Extract nodes, edges, and tags from a PBF file and write as Parquet using osmium.
    """
    pbf_file = Path(pbf_path)
    file_size_mb = pbf_file.stat().st_size / (1024 * 1024)
    logger.info(f"Starting Parquet asset extraction from: {pbf_path} ({file_size_mb:.1f} MB)")
    
    handler = AssetExtractor(output_dir)
    logger.info("Processing PBF file to Parquet buffers (takes a few minutes)...")
    handler.apply_file(pbf_path, locations=True)
    handler.close_files()
    
    return {
        'nodes': handler.node_count,
        'ways': handler.way_count,
        'relations': handler.relation_count,
        'edges': handler.edge_count,
        'tags': handler.tag_count
    }
