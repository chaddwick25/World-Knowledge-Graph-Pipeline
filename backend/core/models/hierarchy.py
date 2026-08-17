"""Hierarchy models for the WorldKG Pipeline.

Holds the OSM/Wikidata administrative hierarchy primitive that drives
config-driven pipeline behaviour (CDD §2.9).
"""
from django.db import models
from django.contrib.postgres.fields import ArrayField
import uuid

from .extraction import PbfFile


class OSMWikiDataHierarchy(models.Model):
    """
    Django primitive for WorldKG Pipeline configuration.
    Aligned with docs/preserving_the_process:
    - Administrative Region as Data Primitive
    - Temporal Geofence (Geofabrik versioning)
    - Cartesian-Semantic Synchronization
    """
    
    class StageType(models.TextChoices):
        PLANET_INITIALIZATION = 'PLANET_INITIALIZATION', 'Planet Initialization'
        PREPROCESSING = 'PREPROCESSING', 'Preprocessing'
        SEMANTIC = 'SEMANTIC', 'Semantic Processing'
        ALIGNMENT = 'ALIGNMENT', 'Knowledge Graph Alignment'
        SPATIAL = 'SPATIAL', 'Spatial Link Prediction'
        VALIDATION = 'VALIDATION', 'Quality Validation'
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # OSM identifiers
    osm_relation_id = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="OSM relation ID for this administrative boundary"
    )
    admin_level = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="OSM admin_level (2=country, 4=state, 6=municipality, etc.)"
    )
    
    # Wikidata identifiers
    wikidata_id = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        db_index=True,
        help_text="Wikidata entity ID (e.g., 'Q781')"
    )
    wikidata_uri = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Full Wikidata URI (e.g., 'http://www.wikidata.org/entity/Q781')"
    )
    
    # Geospatial hierarchy
    name = models.CharField(max_length=255, db_index=True)
    slug = models.CharField(max_length=255, db_index=True)
    parent_slug = models.CharField(max_length=255, null=True, blank=True)
    
    # Continent reference
    continent_name = models.CharField(max_length=100, null=True, blank=True)
    continent_id = models.UUIDField(null=True, blank=True)
    
    # Geofabrik primitive (temporal data source)
    pbf_url = models.URLField(max_length=1024, null=True, blank=True)
    
    # Processing state (Django primitive)
    is_processed = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    
    # Pickle generation capability (constraint for processing eligibility)
    can_generate_pickle = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this region can successfully generate pickle files for subgraph processing"
    )
    pickle_generation_validated_at = models.DateTimeField(null=True, blank=True)
    
    # Pipeline configuration
    default_levels = ArrayField(
        models.IntegerField(),
        default=list,
        blank=True,
        help_text="Default admin levels for this region"
    )
    
    # Processing policy schema (aligned with preserving_the_process)
    processing_policy = models.JSONField(
        default=dict,
        help_text="Policy schema defining pipeline stages, parameters, and prerequisites"
    )
    
    # Metadata
    source_json_path = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Hierarchy cache tracking
    hierarchy_synced_at = models.DateTimeField(null=True, blank=True)
    hierarchy_cache_path = models.CharField(max_length=500, null=True, blank=True)
    subgraph_hierarchy_path = models.CharField(max_length=500, null=True, blank=True)
    
    # Links to existing hierarchy
    region_hierarchy = models.OneToOneField(
        'RegionHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='osm_wikidata_hierarchy',
        help_text="Link to RegionHierarchy for continent recipe integration"
    )
    
    class Meta:
        db_table = 'osm_wikidata_hierarchy'
        unique_together = ('slug', 'admin_level')
        indexes = [
            models.Index(fields=['osm_relation_id']),
            models.Index(fields=['wikidata_id']),
            models.Index(fields=['admin_level']),
            models.Index(fields=['is_processed']),
            models.Index(fields=['can_generate_pickle']),
            models.Index(fields=['slug', 'admin_level']),
        ]
        ordering = ['admin_level', 'name']
    
    def get_pbf_type(self):
        # TODO: add comment that talks about how this structure comes from the wikidata/SPARQL 
        """Map admin_level to PbfFile type."""
        admin_level_map = {
            None: PbfFile.ExtractionLevel.PLANET,
            1: PbfFile.ExtractionLevel.CONTINENT,
            2: PbfFile.ExtractionLevel.REGION,
            4: PbfFile.ExtractionLevel.REGION,
            6: PbfFile.ExtractionLevel.REGION,
            8: PbfFile.ExtractionLevel.REGION_YEARLY,
        }
        return admin_level_map.get(self.admin_level, PbfFile.ExtractionLevel.REGION_CUSTOM)
    def get_default_policy(self):
        """Generate default processing policy based on admin_level."""
        from django.conf import settings
        
        base_dir = settings.BASE_DATA_DIR
        project_root = settings.PROJECT_ROOT
        if self.admin_level is None:  # Planet
            return {
                "stages": {
                    "planet_initialization": {
                        "stage_type": self.StageType.PLANET_INITIALIZATION,
                        "enabled": True,
                        "prerequisites": [],
                        "file_structure": {
                            "base_dir": base_dir,
                            "directories": {
                                "planet_pbf": str(Path(settings.PLANET_OSM_FILE_PATH).parent) if settings.PLANET_OSM_FILE_PATH else "OSM-PBF-FILES/planet/",
                                "osm_wikidata_extractions": str(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) if settings.OSM_WIKIDATA_EXTRACTIONS_DIR else "OSM-PBF-FILES/osm_wikidata_extractions/",
                                "polyfiles": str(settings.POLYGON_FILES_DIR) if settings.POLYGON_FILES_DIR else "data/osm_polygon_files/",
                                "embeddings": str(settings.EMBEDDINGS_ROOT) if settings.EMBEDDINGS_ROOT else "embeddings/",
                                "pickles": "pickles/",
                                "logs": str(settings.LOGS_DIR) if settings.LOGS_DIR else f"{project_root}/logs/"
                            }
                        },
                        "parameters": {
                            "planet_pbf_path": settings.PLANET_OSM_FILE_PATH,
                            "use_existing_planet": True,
                            "temporal_snapshots": False,
                            "extract_continents": True,
                            "continents": ["africa", "asia", "europe", "north-america", "south-america", "australia", "antarctica", "oceania"]
                        }
                    }
                }
            }
        elif self.admin_level == 2:  # Country
            return {
                "stages": {
                    "preprocessing": {
                        "stage_type": self.StageType.PREPROCESSING,
                        "enabled": True,
                        "artifacts": ["polyfile", "country_pickle"],
                        "subgraph_mode": False,
                        "prerequisites": ["planet_initialization"],
                        "polyfile_source": "pbf_snapshot",
                        "validation": {
                            "require_pickle_generation": True,
                            "skip_if_pickle_fails": True
                        }
                    },
                    "load_ontology": {
                        "stage_type": self.StageType.PREPROCESSING,
                        "enabled": True,
                        "prerequisites": ["preprocessing"],
                        "parameters": {"ontology_file": "data/worldkg_ontology_sample.json"}
                    },
                    "train_gv_nle": {
                        "stage_type": self.StageType.SEMANTIC,
                        "enabled": True,
                        "mode": "subgraph",
                        "prerequisites": ["load_ontology", "preprocessing"],
                        "parameters": {"k": 50, "gpu": True, "batch_size": 25000}
                    },
                    "harvest_wikidata": {
                        "stage_type": self.StageType.ALIGNMENT,
                        "enabled": True,
                        "prerequisites": ["train_gv_nle"],
                        "parameters": {"limit": 5000, "generate_embeddings": True, "run_igea": True}
                    },
                    "predict_spatial_links": {
                        "stage_type": self.StageType.SPATIAL,
                        "enabled": True,
                        "mode": "per_province",
                        "prerequisites": ["harvest_wikidata"],
                        "parameters": {"max_heads": 10000, "limit": 100000, "threshold": 0.6, "top_k": 5}
                    }
                }
            }
        elif self.admin_level == 4:  # State/Province (Subgraph)
            return {
                "stages": {
                    "preprocessing": {
                        "stage_type": self.StageType.PREPROCESSING,
                        "enabled": True,
                        "artifacts": ["polyfile", "subgraph_pickle"],
                        "subgraph_mode": True,
                        "prerequisites": ["country_initialized"],
                        "polyfile_source": "pbf_snapshot",
                        "validation": {
                            "require_pickle_generation": True,
                            "skip_if_pickle_fails": True
                        }
                    },
                    "train_gv_nle": {
                        "stage_type": self.StageType.SEMANTIC,
                        "enabled": True,
                        "mode": "subgraph",
                        "prerequisites": ["preprocessing"],
                        "parameters": {"k": 50, "gpu": True, "batch_size": 25000}
                    }
                }
            }
        return {}
    
    def __str__(self):
        return f"{self.name} (admin_level={self.admin_level})"
