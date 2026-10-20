import axios from 'axios'

/**
 * GET /system/status/ — one bootstrap ping shared by HomeV2, the
 * section indexes, and the surface shells: readiness + snapshot
 * dates + the processed-countries set that drives the queryable dots.
 */
export async function fetchSystemStatus() {
  const { data } = await axios.get('/system/status/')
  return {
    ready: !!data.ready,
    snapshotDates: data.snapshot_dates || [],
    defaultDate: data.default || null,
    completedDates: data.completed_dates || [],
    processedCountries: new Set(data.processed_countries || []),
    suggestedPlanetFilePath: data.suggested_planet_file_path || '',
    error: '',
  }
}

/**
 * GET /recipes/regions-map-data/ — country metadata for the section
 * indexes and :cc resolution (same payload the map renders; geometry
 * is ignored by the pickers).
 */
export async function fetchCountries() {
  const { data } = await axios.get('/recipes/regions-map-data/')
  return (data.countries || []).map((c) => ({
    id: c.id,
    name: c.name,
    iso_code: c.iso_code || '',
    continent: c.continent,
    is_geovectors_supported: c.is_geovectors_supported,
  }))
}

/**
 * Slug form matching the backend's normalize_country_slug (lowercase,
 * spaces/hyphens to underscores) — the form Step 6 stores in
 * CountrySearchProcessing.country_name.
 */
export function countrySlug(name) {
  return name.toLowerCase().replace(/ /g, '_').replace(/-/g, '_')
}
