import logging
from pathlib import Path
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from django.conf import settings
from api.models import AssetBundle

logger = logging.getLogger(__name__)

class SparkDeduplicationService:
    """
    High-performance deduplication of OSM datasets using Apache Spark.
    
    This service implements the 'Silver Layer' logic:
    1. Loads raw Parquet extracts (Bronze).
    2. Groups by osm_id and osm_type.
    3. Selects the row with the maximum version/timestamp.
    4. Writes clean snapshots back to Parquet for embedding training.
    """

    def __init__(self, master="local[*]", app_name="WorldKG-Deduplication"):
        self.spark = SparkSession.builder \
            .appName(app_name) \
            .master(master) \
            .config("spark.driver.memory", "64g") \
            .config("spark.executor.memory", "64g") \
            .config("spark.sql.parquet.compression.codec", "snappy") \
            .get_session()

    def deduplicate_bundle(self, bundle_id, output_path=None):
        """
        Deduplicates a specific AssetBundle and saves it to a clean location.
        """
        bundle = AssetBundle.objects.get(id=bundle_id)
        input_dir = Path(bundle.bundle_path)
        
        if not output_path:
            output_path = input_dir.parent / f"{input_dir.name}_cleaned"
        
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Starting Spark deduplication for bundle {bundle_id}")
        logger.info(f"Input:  {input_dir}")
        logger.info(f"Output: {output_path}")

        try:
            # 1. Deduplicate Tags (most important for vocabulary)
            tags_df = self.spark.read.parquet(str(input_dir / "tags.parquet"))
            # If version/timestamp exists, use it. Otherwise just drop duplicates.
            if "version" in tags_df.columns:
                clean_tags = tags_df.withColumn(
                    "rn", F.row_number().over(
                        F.Window.partitionBy("osm_id", "key").orderBy(F.desc("version"))
                    )
                ).filter("rn = 1").drop("rn")
            else:
                clean_tags = tags_df.dropDuplicates(["osm_id", "key"])
            
            clean_tags.write.mode("overwrite").parquet(str(output_path / "tags.parquet"))
            
            # 2. Deduplicate Nodes (Spatial geometry)
            nodes_df = self.spark.read.parquet(str(input_dir / "nodes.parquet"))
            if "version" in nodes_df.columns:
                clean_nodes = nodes_df.dropDuplicates(["osm_id", "version"]) \
                    .withColumn("rn", F.row_number().over(
                        F.Window.partitionBy("osm_id").orderBy(F.desc("version"))
                    )).filter("rn = 1").drop("rn")
            else:
                clean_nodes = nodes_df.dropDuplicates(["osm_id"])
                
            clean_nodes.write.mode("overwrite").parquet(str(output_path / "nodes.parquet"))

            logger.info(f"Successfully created clean snapshot at {output_path}")
            return str(output_path)

        except Exception as e:
            logger.error(f"Spark deduplication failed: {e}", exc_info=True)
            return None

    def stop(self):
        if self.spark:
            self.spark.stop()
