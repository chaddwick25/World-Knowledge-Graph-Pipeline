from django.db import models
import uuid
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django_prometheus.models import ExportModelOperationsMixin



class CountryRelationSnapshot(models.Model):
    """Snapshot of legacy country_relations.json for a given planet snapshot.

    One row per (planet_snapshot, ISO code). This provides a relational view of
    the JSON used during initialization, suitable for visualization and
    inspecting OSM/Wikidata/Geofabrik metadata alongside the hierarchy models.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    planet_snapshot = models.ForeignKey(
        'core.PlanetSnapshot',
        on_delete=models.CASCADE,
        related_name="country_relations",
    )

    # Identity
    iso_code = models.CharField(max_length=3, db_index=True)
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=255, db_index=True)

    # Hierarchy
    parent_slug = models.CharField(max_length=255, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    continent_id = models.UUIDField(null=True, blank=True)

    # OSM / Wikidata / Geofabrik
    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    geofabrik_pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    # GeoVectors TSV paths (if present in the JSON overlay)
    geovectors_location_tsv = models.CharField(max_length=1024, null=True, blank=True)
    geovectors_tags_tsv = models.CharField(max_length=1024, null=True, blank=True)

    # Raw JSON payload for forward-compatibility / auditing
    raw_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "country_relation_snapshots"
        unique_together = ("planet_snapshot", "iso_code")
        indexes = [
            models.Index(fields=["planet_snapshot", "iso_code"]),
            models.Index(fields=["continent_name"]),
        ]

    def __str__(self):
        return f"CountryRelationSnapshot({self.iso_code}, {self.name})"


class ContinentProfile(models.Model):
    """One row per (planet_snapshot, continent) for spatial preprocessing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    planet_snapshot = models.ForeignKey(
        'core.PlanetSnapshot',
        on_delete=models.CASCADE,
        related_name='continents',
    )

    slug = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=255, null=True, blank=True)

    geofabrik_slug = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    regional_state = models.ForeignKey(
        'core.RegionalExtractionState',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )
    continent_pbf = models.ForeignKey(
        'core.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )
    continent_poly = models.ForeignKey(
        'core.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continent_profiles',
    )

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.BigIntegerField(null=True, blank=True, help_text='OSM nodes in continent extract')
    way_count = models.BigIntegerField(null=True, blank=True, help_text='OSM ways in continent extract')
    relation_count = models.BigIntegerField(null=True, blank=True, help_text='OSM relations in continent extract')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Continent PBF file size')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'continent_profiles'
        unique_together = ('planet_snapshot', 'slug')
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['planet_snapshot', 'slug']),
        ]

    def __str__(self):
        return f"{self.slug} ({self.planet_snapshot.snapshot_date})"


class CountryPipelineProfile(models.Model):
    """Canonical, embedding-driven country configuration.

    One row per country that has embeddings available in cold storage.
    """

    class MetadataStatus(models.TextChoices):
        OK = 'OK', 'OK'
        MISSING_GEOFABRIK = 'MISSING_GEOFABRIK', 'Missing Geofabrik metadata'
        MISSING_WIKIDATA = 'MISSING_WIKIDATA', 'Missing Wikidata hierarchy'
        ORPHAN_EMBEDDINGS = 'ORPHAN_EMBEDDINGS', 'Embeddings without hierarchy metadata'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs manual review'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Hierarchy anchors
    planet_snapshot = models.ForeignKey(
        'core.PlanetSnapshot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    continent_profile = models.ForeignKey(
        ContinentProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )

    # Identity
    iso2 = models.CharField(max_length=2, null=True, blank=True, db_index=True)
    iso3 = models.CharField(max_length=3, null=True, blank=True, db_index=True)
    canonical_name = models.CharField(max_length=255)
    canonical_slug = models.CharField(max_length=255, db_index=True)

    # Embedding side
    embedding_slug = models.CharField(
        max_length=255,
        unique=True,
        help_text="Directory or file slug under EMBEDDINGS_ROOT used for this country's embeddings.",
    )
    embedding_root_path = models.CharField(
        max_length=1024,
        help_text="Relative path from EMBEDDINGS_ROOT to this country's embedding directory.",
    )
    has_embeddings = models.BooleanField(default=True, db_index=True)

    # Overrides (from overrides.json)
    has_override = models.BooleanField(default=False, db_index=True)
    override_country_slug = models.CharField(max_length=255, null=True, blank=True)
    override_poly_slug = models.CharField(max_length=255, null=True, blank=True)
    override_embedding_slug = models.CharField(max_length=255, null=True, blank=True)
    override_source = models.CharField(max_length=50, null=True, blank=True)
    overrides_synced_at = models.DateTimeField(null=True, blank=True)
    raw_override_record = models.JSONField(default=dict, blank=True)

    # Pre-computed canonical paths (set by prebuild_country_paths command)
    embedding_location_tsv_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to locations.tsv.gz under EMBEDDINGS_ROOT",
    )
    embedding_tags_tsv_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to tags.tsv.gz under EMBEDDINGS_ROOT",
    )
    snapshot_pbf_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the temporal snapshot .osm.pbf",
    )
    snapshot_poly_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the temporal snapshot .osm.poly",
    )
    pickle_dir = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the pickle directory for wdw.pickle",
    )
    continent_pbf_path = models.CharField(
        max_length=2048,
        null=True,
        blank=True,
        help_text="Resolved absolute path to the continent .pbf this country is extracted from",
    )

    # OSM / Wikidata / Geofabrik side
    osm_wikidata_hierarchy = models.ForeignKey(
        'core.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
        help_text="Linked country-level OSM/Wikidata hierarchy (admin_level=2).",
    )

    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_id = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    continent_id = models.UUIDField(null=True, blank=True)

    geofabrik_slug = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    geofabrik_parent_slug = models.CharField(max_length=255, null=True, blank=True)
    geofabrik_pbf_url = models.URLField(max_length=1024, null=True, blank=True)

    regional_state = models.ForeignKey(
        'core.RegionalExtractionState',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    country_pbf = models.ForeignKey(
        'core.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )
    country_poly = models.ForeignKey(
        'core.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='country_profiles',
    )

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.IntegerField(null=True, blank=True, help_text='OSM nodes in country extract')
    way_count = models.IntegerField(null=True, blank=True, help_text='OSM ways in country extract')
    relation_count = models.IntegerField(null=True, blank=True, help_text='OSM relations in country extract')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Country PBF file size')
    country_relations_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional snapshot of legacy country_relations.json entry for this country.",
    )
    # Subgraph gate
    has_subgraphs = models.BooleanField(default=False, db_index=True)
    # ── Temporal sharding (Phase 1+: snapshot-aware paths) ──
    snapshot_date = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        db_index=True,
        help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31'). "
                  "When set, paths resolve under continents/{snapshot_date}/.",
    )
    # Status / diagnostics
    # TODO: I am going to need  change the shape of this field once we fixed the temporal snapshot issues 
    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.OK,
        db_index=True,
    )
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        db_table = 'country_pipeline_profiles'
        indexes = [
            models.Index(fields=['iso3']),
            models.Index(fields=['canonical_slug']),
            models.Index(fields=['metadata_status']),
            models.Index(fields=['has_subgraphs']),
        ]

    def __str__(self):
        code = self.iso3 or self.iso2 or self.embedding_slug
        return f"{self.canonical_name} ({code})"


class SubgraphProfile(models.Model):
    """Canonical subgraph (admin region / city) configuration per country.

    Links to CountryPipelineProfile and, where possible, to OSM/Wikidata hierarchy
    and PolygonFile / subgraph artifacts.
    """

    class MetadataStatus(models.TextChoices):
        OK = 'OK', 'OK'
        ORPHAN_POLY = 'ORPHAN_POLY', 'Polygon without hierarchy metadata'
        MISSING_RELATION = 'MISSING_RELATION', 'Missing OSM relation'
        NO_PICKLE = 'NO_PICKLE', 'Subgraph pickle missing'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs manual review'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_profile = models.ForeignKey(
        CountryPipelineProfile,
        on_delete=models.CASCADE,
        related_name='subgraphs',
    )
    # Identity
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=255)
    # Hierarchy / Wikidata
    osm_wikidata_hierarchy = models.ForeignKey(
        'core.OSMWikiDataHierarchy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
        help_text="Linked subgraph-level hierarchy (e.g., admin_level 4/6).",
    )
    admin_level = models.IntegerField(null=True, blank=True, db_index=True)
    osm_relation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wikidata_id = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    wikidata_uri = models.CharField(max_length=200, null=True, blank=True)
    continent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Polygon / filesystem linkage
    polygon_file = models.ForeignKey(
        'core.PolygonFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
    )
    subgraph_pbf = models.ForeignKey(
        'core.PbfFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subgraph_profiles',
    )
    polygon_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_pbf_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_poly_path = models.CharField(max_length=1024, null=True, blank=True)
    subgraph_pickle_path = models.CharField(max_length=1024, null=True, blank=True)

    bbox_min_lon = models.FloatField(null=True, blank=True)
    bbox_min_lat = models.FloatField(null=True, blank=True)
    bbox_max_lon = models.FloatField(null=True, blank=True)
    bbox_max_lat = models.FloatField(null=True, blank=True)

    # Osmium fileinfo metrics (pre-computed during pre-build)
    node_count = models.IntegerField(null=True, blank=True, help_text='OSM nodes in subgraph')
    way_count = models.IntegerField(null=True, blank=True, help_text='OSM ways in subgraph')
    relation_count = models.IntegerField(null=True, blank=True, help_text='OSM relations in subgraph')
    file_size_bytes = models.BigIntegerField(null=True, blank=True, help_text='Subgraph PBF file size')

    # Status / diagnostics
    has_subgraph_pbf = models.BooleanField(default=False)
    has_subgraph_poly = models.BooleanField(default=False)
    has_subgraph_pickle = models.BooleanField(default=False)

    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.OK,
        db_index=True,
    )
    notes = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'subgraph_profiles'
        unique_together = ('country_profile', 'slug')
        indexes = [
            models.Index(fields=['country_profile', 'slug']),
            models.Index(fields=['admin_level']),
            models.Index(fields=['osm_relation_id']),
            models.Index(fields=['metadata_status']),
        ]

    def __str__(self):
        return f"{self.country_profile.canonical_name} / {self.name}"


# ══════════════════════════════════════════════════════════════════════════
# EligibleCountry (Init Pipeline eligibility & readiness)
# ══════════════════════════════════════════════════════════════════════════

class EligibleCountry(models.Model):
    """Tracks which countries are eligible for the pipeline per snapshot.

    Populated by the ``scan_embeddings`` management command.  Drives the map
    colouring (green READY / yellow NEEDS_SPLIT / orange NEEDS_MERGE / grey
    NO_EMBEDDINGS) and the init pipeline country-selection UI.

    One row per ``(country_name, snapshot_date)`` pair.  ``country_name`` is
    the human-readable name (e.g. ``"Scotland"``, ``"France"``) — not a slug
    or ISO code — because the frontend uses display names everywhere.
    """

    class EmbeddingStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending Scan'
        READY = 'READY', 'Embeddings Ready'
        NEEDS_SPLIT = 'NEEDS_SPLIT', 'Needs TSV Splitting'
        NEEDS_MERGE = 'NEEDS_MERGE', 'Needs TSV Merging'
        NO_EMBEDDINGS = 'NO_EMBEDDINGS', 'No Embeddings Found'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Identity — loosely coupled; no FK to CountryPipelineProfile
    # because countries may be eligible (or pending scan) before a profile exists.
    country_name = models.CharField(max_length=255, db_index=True)
    iso_code = models.CharField(max_length=10, null=True, blank=True, db_index=True)
    continent = models.CharField(max_length=100)
    # Snapshot context
    snapshot_date = models.CharField(
        max_length=10, default='2025_12_31', db_index=True,
        help_text='YYYY_MM_DD format snapshot date',
    )
    # Embedding status
    embedding_status = models.CharField(
        max_length=20, choices=EmbeddingStatus.choices,
        default=EmbeddingStatus.PENDING,
    )

    # Discovered paths (from scan)
    location_tsv_path = models.CharField(max_length=1024, null=True, blank=True)
    tags_tsv_path = models.CharField(max_length=1024, null=True, blank=True)
    pickle_path = models.CharField(max_length=1024, null=True, blank=True)

    # Split/merge metadata — populated only when NEEDS_SPLIT or NEEDS_MERGE
    source_tsv_name = models.CharField(
        max_length=255, null=True, blank=True,
        help_text='If NEEDS_SPLIT, parent multi-country TSV name (e.g. great-britain-location)',
    )
    source_continent = models.CharField(
        max_length=100, null=True, blank=True,
        help_text='If NEEDS_SPLIT, continent of the source TSV directory',
    )
    needs_merge_regions = ArrayField(
        models.CharField(max_length=100), default=list, blank=True,
        help_text='If NEEDS_MERGE, region slugs to concatenate (e.g. [us-midwest, us-northeast, …])',
    )

    # Init pipeline state
    is_initialized = models.BooleanField(default=False)
    initialized_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    scanned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'eligible_countries'
        unique_together = ('country_name', 'snapshot_date')
        indexes = [
            models.Index(fields=['embedding_status']),
            models.Index(fields=['is_initialized', 'snapshot_date']),
            models.Index(fields=['continent', 'snapshot_date']),
        ]
        verbose_name = 'Eligible Country'
        verbose_name_plural = 'Eligible Countries'

    def __str__(self):
        return f'{self.country_name} ({self.snapshot_date}) [{self.embedding_status}]'


# ══════════════════════════════════════════════════════════════════════════
# PartitionRegistry (Temporal Sharding Phase 0)
# ══════════════════════════════════════════════════════════════════════════


