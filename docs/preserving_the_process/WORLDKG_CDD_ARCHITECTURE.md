***REMOVED*** WorldKG Config-Driven Development (CDD) Architecture

How the WorldKG pipeline implements a **Config-Driven Development (CDD)** paradigm, where OSM (Geofabrik) and Wikidata serve as the foundational configuration sources that instantiate a traditional software database, which then wraps Data Science service lines as a SaaS platform, all aligned with the WorldKG research vision.

---

***REMOVED******REMOVED*** 1. The CDD Paradigm: Configuration as Code

***REMOVED******REMOVED******REMOVED*** Traditional vs. CDD Approach

**Traditional Software Development:**
- Hardcoded business logic in application code
- Manual database schema definitions
- Static service boundaries
- Tight coupling between data and processing

**Config-Driven Development (CDD):**
- **External Configuration Sources**: OSM (Geofabrik) and Wikidata serve as living configuration
- **Dynamic Database Instantiation**: Database schema and content derived from config sources
- **Service Line Abstraction**: Data Science services wrapped as SaaS endpoints
- **Loose Coupling**: Pipeline stages operate on config-derived ground truth

***REMOVED******REMOVED******REMOVED*** The Two Configuration Pillars

***REMOVED******REMOVED******REMOVED******REMOVED*** Pillar 1: OSM/Geofabrik (Cartesian Configuration)
- **Source**: Geofabrik PBF files and index-v1.json
- **What it Configures**: Physical boundaries, spatial extents, administrative hierarchies
- **Format**: Binary PBF streams + JSON metadata
- **Purpose**: Defines the "WHERE" of the system, physical geography

***REMOVED******REMOVED******REMOVED******REMOVED*** Pillar 2: Wikidata (Semantic Configuration)
- **Source**: Wikidata SPARQL endpoint (https://query.wikidata.org/)
- **What it Configures**: Ontological hierarchies, entity identities, semantic relationships
- **Format**: RDF triples via SPARQL queries
- **Purpose**: Defines the "WHAT" of the system, semantic meaning

---

***REMOVED******REMOVED*** 2. Configuration Ingestion: From External Sources to Database

***REMOVED******REMOVED******REMOVED*** Stage 1: Initial Configuration Harvest

The pipeline begins by ingesting configuration from both pillars:

**OSM Configuration Ingestion:**
```python
***REMOVED*** Geofabrik index-v1.json serves as the Cartesian config manifest
{
  "monaco": {
    "path": "europe/monaco.osm.pbf",
    "bbox": [7.4, 43.7, 7.5, 43.8]
  }
}
```

**Wikidata Configuration Ingestion:**
```python
***REMOVED*** SPARQL query harvests semantic config for each OSM region
SELECT ?item ?itemLabel ?coord ?osmRelationId
WHERE {
  ?item wdt:P625 ?coord .
  ?item wdt:P402 ?osmRelationId .
  ***REMOVED*** Spatial bounding box from OSM config
  FILTER(geof:longitude(?coord) >= {min_lon} && ...)
}
```

***REMOVED******REMOVED******REMOVED*** Stage 2: Configuration Fusion (The Algebraic Bridge)

The critical innovation is fusing these two configuration streams into a unified database model:

**OSMWikiDataHierarchy Model:**
```python
class OSMWikiDataHierarchy(models.Model):
    ***REMOVED*** Cartesian config from Geofabrik
    name = models.CharField(max_length=200)  ***REMOVED*** "Monaco"
    slug = models.CharField(max_length=200)  ***REMOVED*** "monaco"
    parent_slug = models.CharField(max_length=200, null=True)  ***REMOVED*** parent hierarchy
    continent_name = models.CharField(max_length=100)  ***REMOVED*** "Europe"
    pbf_url = models.URLField()  ***REMOVED*** Geofabrik PBF download URL
    osm_relation_id = models.BigIntegerField()  ***REMOVED*** OSM relation ID

    ***REMOVED*** Semantic config from Wikidata
    wikidata_id = models.CharField(max_length=50)  ***REMOVED*** "Q235"
    wikidata_uri = models.CharField(max_length=200)  ***REMOVED*** "http://www.wikidata.org/entity/Q235"

    ***REMOVED*** Hierarchical config
    admin_level = models.IntegerField()  ***REMOVED*** 2 (country), 4 (state)
    is_processed = models.BooleanField(default=False)
    can_generate_pickle = models.BooleanField(default=False)
```

**Management Command:**
```bash
python manage.py import_country_relations
```

This command:
1. Reads Geofabrik index-v1.json (Cartesian config)
2. Queries Wikidata SPARQL endpoint (Semantic config)
3. Fuses them into `OSMWikiDataHierarchy` database records
4. Creates the algebraic bridge: $f: Cartesian \to Graph$

---

***REMOVED******REMOVED*** 3. Database Instantiation: Config-Driven Traditional Software

***REMOVED******REMOVED******REMOVED*** From Config to Database Schema

The `OSMWikiDataHierarchy` model is not just data, it is the **database schema definition** derived from configuration:

**Traditional Approach:**
```python
***REMOVED*** Hardcoded schema
class Country(models.Model):
    name = models.CharField()
    ***REMOVED*** Manual schema changes require code deployment
```

**CDD Approach:**
```python
***REMOVED*** Config-driven schema
class OSMWikiDataHierarchy(models.Model):
    ***REMOVED*** Schema fields mirror config structure from OSM + Wikidata
    ***REMOVED*** New config sources (e.g., adding a new OSM metadata field)
    ***REMOVED*** automatically extend the schema via migrations
```

***REMOVED******REMOVED******REMOVED*** Database as Ground Truth

Once instantiated, the database becomes the **single source of truth** for all downstream operations:

**HierarchyCacheService:**
```python
class HierarchyCacheService:
    """Builds in-memory hierarchies from config-derived database."""
    def get_or_build_hierarchy(self):
        ***REMOVED*** Reads from OSMWikiDataHierarchy (config-derived DB)
        ***REMOVED*** No JSON file dependencies
        ***REMOVED*** Always reflects latest config state
        countries = OSMWikiDataHierarchy.objects.filter(admin_level=2)
        for country in countries:
            self.hierarchy[country.slug] = {
                'wikidata_uri': country.wikidata_uri,
                'subgraphs': self._get_subgraphs(country)
            }
```

**SubgraphProfile Model:**
```python
class SubgraphProfile(models.Model):
    """Config-derived subgraph metadata."""
    parent = models.ForeignKey(OSMWikiDataHierarchy)
    name = models.CharField()  ***REMOVED*** "Cayo District"
    slug = models.CharField()  ***REMOVED*** "cayo_district"
    bbox = ArrayField(models.FloatField())
    subgraph_pbf_path = models.CharField()  ***REMOVED*** Derived from config
    subgraph_poly_path = models.CharField()  ***REMOVED*** Derived from config
    subgraph_pickle_path = models.CharField()  ***REMOVED*** Derived from config
```

---

***REMOVED******REMOVED*** 4. Service Line Wrapping: Data Science as SaaS

***REMOVED******REMOVED******REMOVED*** Traditional Software Wrapping Data Science

The instantiated database (from config) now serves as the foundation for wrapping Data Science service lines as SaaS endpoints:

**Architecture Layers:**

| Layer | Components |
|-------|-----------|
| **SaaS API Layer (Django REST)** | `/api/worldkg/entities/`, `/api/country-preprocess/` |
| **Data Science Service Lines** | `FastTextEmbeddingService`, `WeightedDeepWalkService` (GV-NLE), `CrossAttentionIGEA`, `TransEGraphEmbeddingService`, `SpatialLinkPredictionService` |
| **Config-Driven Database (PostgreSQL)** | `OSMWikiDataHierarchy` (Config DB), `OsmEntity` (Vector DB), `SpatialTripletScore` (Links DB) |
| **External Configuration Sources** | OSM/Geofabrik (Cartesian Config), Wikidata (Semantic Config) |

Each layer consumes the layer below. The API layer exposes services; services consume the database; the database is instantiated from external config.

***REMOVED******REMOVED******REMOVED*** Service Line Examples

**Service 1: GeoVectors Encoding (SaaS Endpoint)**
```python
***REMOVED*** Frontend-triggered SaaS endpoint
class CountryPreProcessView(APIView):
    def post(self, request):
        country_name = request.data['country_name']

        ***REMOVED*** 1. Lookup config-derived hierarchy
        hierarchy = OSMWikiDataHierarchy.objects.get(name=country_name)

        ***REMOVED*** 2. Invoke Data Science service line
        extraction_service = SnapshotExtractionService()
        extraction_service.run_single_snapshot(
            country_slug=hierarchy.slug,
            bbox=hierarchy.bbox
        )

        ***REMOVED*** 3. Generate subgraphs (config-derived)
        subgraph_service = SubgraphGenerationService()
        subgraph_service.generate_from_hierarchy(hierarchy)

        return Response({"status": "processing"})
```

**Service 2: IGEA Alignment (Terminal SaaS)**
```python
***REMOVED*** Terminal-triggered SaaS command
class Command(BaseCommand):
    def handle(self, *args, **options):
        country_iso = options['country']

        ***REMOVED*** 1. Lookup config-derived metadata
        hierarchy = OSMWikiDataHierarchy.objects.get(iso_code=country_iso)

        ***REMOVED*** 2. Invoke Data Science service line
        wikidata_service = WikidataCandidateService()
        candidates = wikidata_service.harvest_by_bbox(
            bbox=hierarchy.bbox,
            wikidata_uri=hierarchy.wikidata_uri
        )

        ***REMOVED*** 3. Run IGEA alignment
        igea_service = IterativeEntityAlignmentService()
        igea_service.align(candidates, threshold=0.6)
```

**Service 3: USLP Link Prediction (Terminal SaaS)**
```python
***REMOVED*** Terminal-triggered SaaS command with GPU acceleration
class Command(BaseCommand):
    def handle(self, *args, **options):
        country_iso = options['country']

        ***REMOVED*** 1. Lookup config-derived subgraphs
        hierarchy = OSMWikiDataHierarchy.objects.get(iso_code=country_iso)
        subgraphs = SubgraphProfile.objects.filter(parent=hierarchy)

        ***REMOVED*** 2. Invoke Data Science service line (GPU-accelerated)
        for subgraph in subgraphs:
            uslp_service = TorchUSLP(device='cuda:0')
            uslp_service.predict_links(
                polygon_wkt=subgraph.subgraph_poly_path,
                threshold=0.7
            )
```

---

***REMOVED******REMOVED*** 5. Alignment with WorldKG Research Vision

***REMOVED******REMOVED******REMOVED*** WorldKG Research Goals

The [WorldKG project](https://www.vgiscience.org/projects/worldkg.html) aims to:
1. **Scale OSM to a Knowledge Graph**: Transform OSM data into structured KG
2. **Semantic Enrichment**: Add ontological meaning via Wikidata alignment
3. **Spatial-Semantic Fusion**: Bridge physical geography with semantic meaning
4. **Global Coverage**: Support world-scale extraction and processing

***REMOVED******REMOVED******REMOVED*** How the CDD Architecture Achieves This

**Goal 1: Scale OSM to a Knowledge Graph**
- **CDD Implementation**: OSMWikiDataHierarchy model scales to 197 countries with automatic subgraph generation
- **Config Source**: Geofabrik provides the scalable Cartesian config foundation
- **Database Instantiation**: Hierarchical model supports Planet → Continent → Country → Subgraph

**Goal 2: Semantic Enrichment**
- **CDD Implementation**: Wikidata SPARQL queries provide semantic config that fuses with OSM config
- **Service Line**: IGEA (Iterative Geographic Entity Alignment) wraps cross-attention as SaaS
- **Database Storage**: `wkg_class`, `wikidata_uri`, `wkg_superclasses` in OsmEntity model

**Goal 3: Spatial-Semantic Fusion**
- **CDD Implementation**: Algebraic bridge (OSM Relation ID) fuses Cartesian + Semantic config
- **Service Line**: GeoVectors (GV-Tags + GV-NLE) wraps embedding generation as SaaS
- **Database Storage**: `gv_tags_embedding` (300D) + `gv_nle_embedding` (100D) in pgvector

**Goal 4: Global Coverage**
- **CDD Implementation**: Config-driven approach supports any region with OSM + Wikidata coverage
- **Service Line**: Celery canvas pipeline with frontend/terminal triggers
- **Database Instantiation**: PipelineRun model tracks progress across all stages

---

***REMOVED******REMOVED*** 6. The CDD Advantage

***REMOVED******REMOVED******REMOVED*** Why Config-Driven Development?

**1. Adaptability to External Changes**
- OSM updates (new PBF releases) → Config change → Database update → No code changes
- Wikidata updates (new entities) → Config change → Database update → No code changes

**2. Reproducibility**
- Config sources are versioned (Geofabrik timestamps, Wikidata entity versions)
- Database state can be recreated from specific config snapshots
- Pipeline runs are deterministic based on config version

**3. Extensibility**
- New config sources (e.g., OpenStreetMap tags, custom ontologies) → Extend model → Extend service lines
- New service lines (e.g., new ML models) → Wrap as SaaS → No database changes needed
- New pipeline stages → Add to PipelineRun model → Integrate with existing config

**4. Separation of Concerns**
- **Config Layer**: OSM + Wikidata (external truth)
- **Database Layer**: Django models (instantiated from config)
- **Service Layer**: Data Science algorithms (wrapped as SaaS)
- **API Layer**: REST endpoints (expose services)

---

***REMOVED******REMOVED*** 7. Pipeline Stage Alignment

All pipeline stages align through the CDD architecture:

| Stage | Config Source | Database Model | Service Line | SaaS Endpoint |
|-------|---------------|----------------|--------------|---------------|
| **0. Planet Init** | Geofabrik planet PBF | PlanetSnapshot, RegionHierarchy | SnapshotExtractionService | Frontend |
| **1. Embed OSM Entities** | OSMWikiDataHierarchy | OsmEntity (gv_tags_embedding) | FastTextEmbeddingService | Frontend → Celery |
| **1b. WorldKG Enrichment** | WorldKG TTL ontology | OsmEntity (wkg_class) | WorldKGEnrichmentService | Celery |
| **2. Harvest Wikidata** | OSMWikiDataHierarchy, Wikidata SPARQL | OsmEntity (wikidata_uri) | WikidataCandidateService | Celery |
| **3. Run IGEA** | OSMWikiDataHierarchy | OsmEntity (wikidata_uri, wkg_class) | CrossAttentionIGEA | Celery |
| **4. USLP Prediction** | SubgraphProfile | SpatialTripletScore | TorchUSLP | Celery |
| **5. Train GV-NLE** | SubgraphProfile | OsmEntity (gv_nle_embedding) | WeightedDeepWalkService | Celery |

**Key Insight**: Every stage after database setup consumes config-derived database models and wraps Data Science service lines as SaaS endpoints.

---

***REMOVED******REMOVED*** 8. Conclusion

The WorldKG pipeline implements a **Config-Driven Development (CDD)** paradigm where:

1. **OSM (Geofabrik) + Wikidata** serve as the foundational configuration sources
2. **Configuration fusion** creates the algebraic bridge between Cartesian and Semantic spaces
3. **Database instantiation** creates a traditional software database from config
4. **Service line wrapping** exposes Data Science algorithms as SaaS endpoints
5. **WorldKG alignment** ensures all stages contribute to the global Knowledge Graph vision

This architecture preserves the mathematical intent of the WorldKG research while providing a scalable, adaptable, and reproducible platform for geospatial vector search at global scale.
