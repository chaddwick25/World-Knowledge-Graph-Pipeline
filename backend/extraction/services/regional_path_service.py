import logging
from pathlib import Path
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


def normalize_country_slug(name: str) -> str:
    """
    Normalize a country name to slug format: lowercase, spaces and hyphens replaced with underscores.
    Example: "United Kingdom" -> "united_kingdom", "Costa Rica" -> "costa_rica"
    """
    return name.lower().replace(' ', '_').replace('-', '_')


def normalize_country_name(name: str) -> str:
    """
    Normalize a country name to display format: lowercase, underscores and hyphens replaced with spaces.
    Example: "united_kingdom" -> "united kingdom", "costa-rica" -> "costa rica"
    """
    return name.lower().replace('_', ' ').replace('-', ' ')


def normalize_continent_slug(name) -> str:
    """
    Normalize a continent name to slug format: lowercase, spaces and hyphens replaced with underscores.
    Standardizes continent naming to match database convention (underscores).
    Example: "North America" -> "north_america", "north-america" -> "north_america"

    Returns empty string if name is None or empty.
    """
    if not name:
        return ""
    return name.lower().replace(' ', '_').replace('-', '_')

class RegionalPathService:
    """
    Service for managing regional PBF file paths.
    Resolves continent/region paths based on configured base directory.
    """
    
    def __init__(self, base_dir: str = None):
        # Use explicitly provided base_dir or configured OSM_WIKIDATA_EXTRACTIONS_DIR
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR)

    def get_continent_dir(self, continent: str) -> Path:
        cont_slug = normalize_continent_slug(continent)
        if not cont_slug:
            cont_slug = continent
        path = self.base_dir / cont_slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_country_dir(self, continent: str, country: str) -> Path:
        # Normalize both slugs to underscores for filesystem consistency.
        # This prevents mismatches like cape-verde vs cape_verde, and
        # continental mismatches like australia-oceania vs australia_oceania.
        cont_slug = normalize_continent_slug(continent)
        country_slug = normalize_country_slug(country)
        if not cont_slug:
            cont_slug = continent  # fallback if normalization returned empty
        path = self.get_continent_dir(cont_slug) / country_slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── Continent snapshot paths (Phase 1: temporal sharding) ──────────

    def get_continent_snapshot_root(self) -> Path:
        """Root directory for date-specific continent snapshots.
        
        Example: /data/continents/
        """
        return self.base_dir / "continents"

    def get_continent_snapshot_date_dir(self, snapshot_date: str) -> Path:
        """Directory for a specific continent snapshot date.
        
        Example: /data/continents/2025_12_31/
        """
        return self.get_continent_snapshot_root() / snapshot_date

    def get_continent_snapshot_pbf_path(
        self, continent_slug: str, snapshot_date: str = None
    ) -> Path:
        """Path for a continent PBF at a specific snapshot date.
        
        Example: /data/continents/2025_12_31/europe.pbf
        
        Args:
            continent_slug: e.g. "europe", "north-america"
            snapshot_date: YYYY_MM_DD format. Defaults to SINGLE_SNAPSHOT_DATE.
        """
        if snapshot_date is None:
            snapshot_date = getattr(settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")
        return (
            self.get_continent_snapshot_date_dir(snapshot_date)
            / f"{continent_slug}.pbf"
        )

    def list_available_snapshot_dates(self) -> list:
        """List all available continent snapshot dates on disk.
        
        Scans the continents/ directory for date-named subdirectories.
        Returns sorted list newest-first.
        """
        root = self.get_continent_snapshot_root()
        if not root.exists():
            return []
        dates = sorted(
            (d.name for d in root.iterdir() if d.is_dir() and "_" in d.name),
            reverse=True,
        )
        return dates

    # ── End continent snapshot paths ───────────────────────────────────

    def get_yearly_dir(self, continent: str, country: str) -> Path:
        path = self.get_country_dir(continent, country) / 'monthly'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_snapshot_dir(self, continent: str, country: str) -> Path:
        path = self.get_country_dir(continent, country) / 'temporal_snapshots'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_continent_pbf_path(self, continent: str) -> Path:
        return self.get_continent_dir(continent) / f"{continent}.pbf"

    def get_country_pbf_path(self, continent: str, country: str) -> Path:
        return self.get_country_dir(continent, country) / f"{country}.pbf"

    def get_yearly_pbf_path(self, continent: str, country: str, year: int) -> Path:
        return self.get_yearly_dir(continent, country) / f"{year}.pbf"

    def get_monthly_pbf_path(self, continent: str, country: str, year: int, month: int) -> Path:
        return self.get_monthly_dir(continent, country) / f"{year}_{month:02d}.pbf"

    def get_snapshot_pbf_path(self, continent: str, country: str, year: int, month: int, day: int) -> Path:
        return self.get_snapshot_dir(continent, country) / f"{year}_{month:02d}_{day:02d}.pbf"

    def get_pickle_dir(self, continent: str, country: str) -> Path:
        path = self.get_country_dir(continent, country) / 'pickle'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_pickle_path(self, continent: str, country: str) -> Path:
        """Returns the canonical path to wdw.pickle. Does NOT create parent dirs (use for existence checks)."""
        country_slug = normalize_country_slug(country)
        return self.base_dir / continent / country_slug / 'pickle' / 'wdw.pickle'

    def get_model_dir(self, continent: str, country: str, model_type: str) -> Path:
        """Returns and creates the model output directory. model_type: 'fasttext' or 'nle'"""
        path = self.get_country_dir(continent, country) / 'models' / model_type
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_single_snapshot_pbf_path(self, continent: str, country: str, snapshot_date: str = None) -> Path:
        """
        Returns the path for the single snapshot PBF (e.g., ireland_2025_12_31.osm.pbf).
        snapshot_date format: YYYY_MM_DD (e.g., '2025_12_31')
        """
        if snapshot_date is None:
            snapshot_date = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
        country_slug = normalize_country_slug(country)
        return self.get_snapshot_dir(continent, country) / f"{country_slug}_{snapshot_date}.osm.pbf"

    def get_subgraphs_dir(self, continent: str, country: str) -> Path:
        """Returns and creates the subgraphs directory for a country."""
        path = self.get_country_dir(continent, country) / 'subgraphs'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_subgraph_hierarchy_path(self, continent: str, country: str, iso: str) -> Path:
        """Returns the path for the Wikidata hierarchy JSON under subgraphs (e.g., IE_hierarchy.json)."""
        return self.get_subgraphs_dir(continent, country) / f"{iso.upper()}_hierarchy.json"

    def get_subgraph_dir(self, continent: str, country: str, subgraph_slug: str) -> Path:
        """Returns and creates the directory for a specific subgraph."""
        path = self.get_subgraphs_dir(continent, country) / subgraph_slug.lower()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_subgraph_pbf_path(self, continent: str, country: str, subgraph_slug: str, snapshot_date: str = None) -> Path:
        """
        Returns the path for a subgraph PBF (e.g., ireland_dublin_2025_12_31.osm.pbf).
        snapshot_date format: YYYY_MM_DD
        """
        if snapshot_date is None:
            snapshot_date = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
        return self.get_subgraph_dir(continent, country, subgraph_slug) / f"{subgraph_slug.lower()}_{snapshot_date}.osm.pbf"

    def get_subgraph_poly_path(self, continent: str, country: str, subgraph_slug: str, snapshot_date: str = None) -> Path:
        """
        Returns the path for a subgraph poly file in the regional-extractions directory.
        Format: {country_slug}_{subgraph_slug}_{snapshot_date}.osm.poly
        snapshot_date format: YYYY_MM_DD
        """
        if snapshot_date is None:
            snapshot_date = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
        subgraph_dir = self.get_subgraph_dir(continent, country, subgraph_slug)
        return subgraph_dir / f"{subgraph_slug.lower()}_{snapshot_date}.osm.poly"

    def resolve_country_slug_for_subgraphs(self, continent: str, primary_slug: str, country_name: str, country_data: dict) -> str:
        """
        Resolve the correct country slug for subgraph directory lookup.

        Handles slug mismatches between metadata (e.g., 'ireland-and-northern-ireland')
        and actual filesystem (e.g., 'ireland') by trying fallback variations.

        Args:
            continent: Continent slug (e.g., 'europe')
            primary_slug: Primary slug from metadata (e.g., 'ireland_and_northern_ireland')
            country_name: Human-readable country name (e.g., 'Ireland')
            country_data: Country metadata dict from get_country_by_name

        Returns:
            The first slug that corresponds to an existing directory.
        """
        # Try primary slug first
        primary_dir = self.get_country_dir(continent, primary_slug)
        if primary_dir.exists():
            return primary_slug

        # Fallback 1: Try simple country name (e.g., 'ireland' instead of 'ireland-and-northern-ireland')
        simple_slug = country_name.lower().replace(' ', '_').replace('-', '_')
        if simple_slug != primary_slug:
            simple_dir = self.get_country_dir(continent, simple_slug)
            if simple_dir.exists():
                logger.info(f"Using alternate slug '{simple_slug}' for {country_name}")
                return simple_slug

        # Fallback 2: Try ISO code if available
        iso_code = country_data.get('iso2') or country_data.get('wikidata_id')
        if iso_code:
            iso_slug = iso_code.lower()
            if iso_slug != primary_slug and iso_slug != simple_slug:
                iso_dir = self.get_country_dir(continent, iso_slug)
                if iso_dir.exists():
                    logger.info(f"Using ISO code slug '{iso_slug}' for {country_name}")
                    return iso_slug

        # Return primary slug if no fallback found (will be handled by caller)
        return primary_slug

regional_path_service = RegionalPathService()
