"""
Canonical continent hierarchy — taxonomy + aggregation regression tests.

Ground truth: ``core.services.planet_init.continent_hierarchy`` — exactly
seven canonical continents; every raw hierarchy ``parent_slug`` (Geofabrik
subregions like ``china``/``france``/``united_kingdom``/``australia`` and
naming variants like ``australia-oceania``/``australia_oceania``) must
normalize into the canonical set or ``other``.

The display aggregation (``SystemSummaryView._get_embeddings_info``) must
never surface a raw subregion as a continent.
"""

import pytest

from core.services.planet_init.continent_hierarchy import (
    CANONICAL_SLUGS,
    CONTINENTS,
    ContinentDefinition,
    continent_name,
    normalize_continent,
)


@pytest.mark.unit
class TestContinentTaxonomyShape:
    """The taxonomy itself is the invariant: exactly 7 continents."""

    def test_exactly_seven_continents(self):
        assert len(CONTINENTS) == 7
        assert len(CANONICAL_SLUGS) == 7

    def test_canonical_slugs_are_the_standard_seven(self):
        assert CANONICAL_SLUGS == {
            "africa",
            "antarctica",
            "asia",
            "europe",
            "north-america",
            "oceania",
            "south-america",
        }

    def test_every_definition_is_frozen_and_has_an_alias(self):
        for continent in CONTINENTS:
            assert isinstance(continent, ContinentDefinition)
            assert continent.slug
            assert continent.name
            assert continent.aliases  # each canonical slug is its own alias
            assert continent.slug in continent.aliases


@pytest.mark.unit
class TestNormalizeContinent:
    """Every observed raw parent_slug resolves to the canonical set."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Canonical passthrough
            ("africa", "africa"),
            ("europe", "europe"),
            ("asia", "asia"),
            ("north-america", "north-america"),
            ("south-america", "south-america"),
            # Geofabrik subregions that leaked as continents (observed in DB)
            ("china", "asia"),
            ("france", "europe"),
            ("united_kingdom", "europe"),
            ("great-britain", "europe"),
            ("australia", "oceania"),
            ("central-america", "north-america"),
            # Naming variants
            ("australia-oceania", "oceania"),
            ("australia_oceania", "oceania"),
            ("north_america", "north-america"),
            ("central_america", "north-america"),
            ("south_america", "south-america"),
            ("great_britain", "europe"),
            # Case / whitespace
            ("North America", "north-america"),
            ("  EUROPE  ", "europe"),
            # Unclassified
            (None, "other"),
            ("", "other"),
            ("unknown-region", "other"),
        ],
    )
    def test_normalize_maps_to_canonical(self, raw, expected):
        assert normalize_continent(raw) == expected

    def test_alias_index_derived_from_dataclasses(self):
        # The alias index is built from the dataclasses — no second source.
        for continent in CONTINENTS:
            for alias in continent.aliases:
                assert normalize_continent(alias) == continent.slug

    def test_continent_name_display(self):
        assert continent_name("oceania") == "Oceania"
        assert continent_name("north-america") == "North America"
        assert continent_name("other") == "other"  # fallback bucket


@pytest.mark.unit
@pytest.mark.django_db
class TestSummaryAggregationFoldsAliases:
    """SystemSummaryView._get_embeddings_info must bucket by canonical
    continents only — no raw subregions in the output."""

    SEED = [
        # (hierarchy slug, raw parent_slug, iso2)
        ("zz_hier_mc", "france", "Z1"),
        ("zz_hier_hk", "china", "Z2"),
        ("zz_hier_au_region", "australia", "Z3"),
        ("zz_hier_nz", "australia-oceania", "Z4"),
        ("zz_hier_mx", "central-america", "Z5"),
        ("zz_hier_uk", "united_kingdom", "Z6"),
    ]

    def _seed(self):
        from core.models import CountryPipelineProfile, OSMWikiDataHierarchy

        for hier_slug, parent, iso in self.SEED:
            OSMWikiDataHierarchy.objects.update_or_create(
                slug=hier_slug,
                defaults={"name": hier_slug, "parent_slug": parent},
            )
            CountryPipelineProfile.objects.update_or_create(
                embedding_slug=hier_slug,
                defaults={
                    "canonical_name": hier_slug,
                    "canonical_slug": hier_slug,
                    "iso2": iso,
                    "has_embeddings": True,
                },
            )

    def test_by_continent_has_only_canonical_buckets(self):
        from api.views_system_summary import SystemSummaryView

        self._seed()
        try:
            result = SystemSummaryView()._get_embeddings_info()
        finally:
            from core.models import CountryPipelineProfile, OSMWikiDataHierarchy

            for hier_slug, _, _ in self.SEED:
                OSMWikiDataHierarchy.objects.filter(slug=hier_slug).delete()
                CountryPipelineProfile.objects.filter(embedding_slug=hier_slug).delete()

        names = {b["name"] for b in result["by_continent"]}

        # No raw Geofabrik subregion or naming variant may surface.
        assert not (names & {"france", "china", "australia", "united_kingdom"})
        assert not (names & {"australia-oceania", "australia_oceania", "central-america"})

        # Every bucket is a canonical continent name or the "other" fallback.
        canonical_names = {continent_name(s) for s in CANONICAL_SLUGS}
        assert names <= canonical_names | {"other"}

        # Seeded aliases folded into their canonical buckets.
        by_name = {b["name"]: b for b in result["by_continent"]}
        assert by_name["Oceania"]["total"] >= 2       # australia + australia-oceania
        assert by_name["Asia"]["total"] >= 1          # china
        assert by_name["Europe"]["total"] >= 2        # france + united_kingdom
        assert by_name["North America"]["total"] >= 1 # central-america
