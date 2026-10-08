/**
 * overlayDeckLayers — deck.gl builders for the overlays that migrated off
 * Leaflet (search results, query graph, USLP links, agent overlays).
 *
 * Reuses the existing patterns: the same layer vocabulary as
 * templateDeckLayers (ScatterplotLayer / LineLayer / PathLayer /
 * PolygonLayer / TextLayer), the shared geometry helpers from
 * templateDeckLayers (buildCirclePolygon), and the colors + entity
 * normalization that lived in useMapOverlayLayers.js. Each builder returns
 * a layer array for labelManager.setLayerGroup.
 *
 * Every pickable layer's datum carries the entity fields the info panel
 * reads (name, wkg_class, distance_m, scores, osm_type, osm_id), so the
 * hover tooltip and the EntityInfoPanel work uniformly across sources.
 */

import { GeoJsonLayer, LineLayer, PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'
import { PathStyleExtension } from '@deck.gl/extensions'

import { buildCirclePolygon } from './templateDeckLayers'

// ── Countries (the base interaction layer — deck owns all picking) ──────

/** Uniform country layer (2026-09-28): pickable so deck's native picking
 *  handles country selection; no selection fill (status lives in the
 *  sidebar); unsupported countries render transparent and are skipped in
 *  the click/tooltip guards. */
export function buildCountryLayer(features) {
  return new GeoJsonLayer({
    id: 'countries',
    data: { type: 'FeatureCollection', features: features || [] },
    pickable: true,
    stroked: true,
    filled: true,
    getFillColor: (f) => (
      f.properties.is_geovectors_supported ? [31, 41, 55, 76] : [0, 0, 0, 0]
    ),
    getLineColor: (f) => (
      f.properties.is_geovectors_supported ? [55, 65, 81, 204] : [0, 0, 0, 0]
    ),
    getLineWidth: 1.5,
    lineWidthUnits: 'pixels',
  })
}

// ── Relation type color map (moved from useMapOverlayLayers.js) ─────────
export const RELATION_COLORS = {
  addrCity:        '#8b5cf6',  // violet-500
  addrPlace:       '#10b981',  // emerald-500
  addrNeighbour:   '#06b6d4',  // cyan-500
  addrSuburb:      '#84cc16',  // lime-500
  addrDistrict:    '#3b82f6',  // blue-500
  addrState:       '#ef4444',  // red-500
  addrProvince:    '#ec4899',  // pink-500
  addrHamlet:      '#f59e0b',  // amber-500
}
const DEFAULT_RELATION_COLOR = '#6b7280'  // gray-500 for unknown relations

export function getRelationColor(relation) {
  return RELATION_COLORS[relation] || DEFAULT_RELATION_COLOR
}

// ── Agent overlay tool colors (moved from useMapOverlayLayers.js) ───────
const TOOL_COLORS = {
  structuredSearch: '#3b82f6',  // blue
  templateQuery: '#10b981',     // green
}

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''))
  if (!m) return [99, 102, 241]
  const v = parseInt(m[1], 16)
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

/** Accept either a raw data-tool result or an explicit entities list
 *  (moved from useMapOverlayLayers.js — carries osm fields now). */
export function normalizeOverlayEntities(overlay) {
  const raw = overlay.result || overlay.entities || []
  const source = Array.isArray(raw)
    ? raw
    : raw.results || (raw.result && raw.result.results) || []
  return source
    .map(normalizeOverlayEntity)
    .filter((e) => e.lat != null && e.lon != null)
}

/** First non-null value across the extractor list. `truthy` mode also
 *  skips '' — an empty name field must fall through to tags.name. */
function firstOf(e, pickers, { truthy = false } = {}) {
  for (const pick of pickers) {
    const v = pick(e)
    if (truthy ? v : v != null) return v
  }
  return null
}

/** Per-field fallback extractors — each row is an ordered candidate list;
 *  the field spec is the table, firstOf is the one branch. */
const OVERLAY_ENTITY_FIELDS = {
  lat: { pickers: [(e) => e.lat, (e) => e.geom?.lat] },
  lon: { pickers: [(e) => e.lon, (e) => e.geom?.lon] },
  name: {
    pickers: [(e) => e.name, (e) => e.tags?.name],
    truthy: true,
    fallback: (e) => `${e.osm_type || 'osm'} ${e.osm_id || ''}`.trim(),
  },
  wkg_class: { pickers: [(e) => e.wkg_class, (e) => e.wkgClass], truthy: true },
  osm_type: { pickers: [(e) => e.osm_type], truthy: true },
  osm_id: { pickers: [(e) => e.osm_id] },
  scores: { pickers: [(e) => e.scores], truthy: true },
  score: {
    pickers: [(e) => e.score, (e) => e.scores?.final_score, (e) => e.diffusion_score],
  },
  distance_m: { pickers: [(e) => e.distance_m, (e) => e.distanceM] },
}

function normalizeOverlayEntity(e) {
  return Object.fromEntries(
    Object.entries(OVERLAY_ENTITY_FIELDS).map(([key, spec]) => [
      key,
      firstOf(e, spec.pickers, spec) ?? spec.fallback?.(e) ?? null,
    ])
  )
}

// ── Search results ──────────────────────────────────────────────────────

export function buildSearchResultLayer(results) {
  const rows = (results || [])
    .filter((r) => r.geom && r.geom.lat != null && r.geom.lon != null)
    .map((r) => ({ ...r, position: [r.geom.lon, r.geom.lat] }))
  if (!rows.length) return []
  return [new ScatterplotLayer({
    id: 'search-result-points',
    data: rows,
    getPosition: (d) => d.position,
    getRadius: 18,
    radiusUnits: 'pixels',
    filled: true,
    stroked: true,
    // Search-result entities are indigo/purple (flipped from orange
    // 2026-09-28, matching template entities)
    getFillColor: [99, 102, 241, 230],
    getLineColor: [224, 231, 255, 255],
    lineWidthMinPixels: 1,
    pickable: true,
    autoHighlight: true,
  })]
}

// ── Query graph (anchor/entity + links + distance lines) ────────────────

export function buildQueryGraphLayers(graph) {
  const layers = []
  const anchors = (graph.anchors || [])
    .filter((a) => a.lat != null && a.lon != null)
    .map((a) => ({ ...a, position: [a.lon, a.lat] }))
  const entities = (graph.entities || [])
    .filter((e) => e.geom && e.geom.lat != null)
    .map((e) => ({ ...e, position: [e.geom.lon, e.geom.lat] }))

  if (anchors.length) {
    layers.push(new ScatterplotLayer({
      id: 'query-graph-anchors',
      data: anchors,
      getPosition: (d) => d.position,
      getRadius: 24,
      radiusUnits: 'pixels',
      filled: true,
      stroked: true,
      // Anchors are orange (flipped from indigo 2026-09-28)
      getFillColor: [249, 115, 22, 220],
      getLineColor: [255, 237, 213, 255],
      lineWidthMinPixels: 1,
      pickable: true,
      autoHighlight: true,
    }))
  }
  if (entities.length) {
    layers.push(new ScatterplotLayer({
      id: 'query-graph-entities',
      data: entities,
      getPosition: (d) => d.position,
      getRadius: 18,
      radiusUnits: 'pixels',
      filled: true,
      stroked: true,
      // Entities are indigo/purple (flipped from orange 2026-09-28)
      getFillColor: [99, 102, 241, 220],
      getLineColor: [224, 231, 255, 255],
      lineWidthMinPixels: 1,
      pickable: true,
      autoHighlight: true,
    }))
  }

  // Edges entity → nearest anchor (old Leaflet: dashed #a5b4fc 1.5px).
  const links = (graph.links || [])
    .map((link) => {
      const anchor = graph.anchors?.[link.anchorIdx]
      const entity = graph.entities?.[link.entityIdx]
      if (!anchor || anchor.lat == null || !entity?.geom) return null
      return { path: [[anchor.lon, anchor.lat], [entity.geom.lon, entity.geom.lat]] }
    })
    .filter(Boolean)
  if (links.length) {
    layers.push(new PathLayer({
      id: 'query-graph-links',
      data: links,
      getPath: (d) => d.path,
      getColor: [165, 180, 252, 140],
      widthUnits: 'pixels',
      getWidth: 1.5,
      pickable: false,
      extensions: [new PathStyleExtension({ dash: true })],
      getDashArray: [4, 4],
    }))
  }

  // Distance/bearing lines between two anchors (old Leaflet: solid
  // #818cf8 + divIcon label — same pattern as the template #2 lines).
  const lines = (graph.anchorLines || [])
    .map((line, index) => {
      const from = line.from
      const to = line.to
      if (!from || !to || from.lat == null || to.lat == null) return null
      return {
        id: `qg-line-${index}`,
        path: [[from.lon, from.lat], [to.lon, to.lat]],
        label: line.label || '',
        mid: [(from.lon + to.lon) / 2, (from.lat + to.lat) / 2],
      }
    })
    .filter(Boolean)
  if (lines.length) {
    layers.push(new PathLayer({
      id: 'query-graph-anchor-lines',
      data: lines,
      getPath: (d) => d.path,
      getColor: [129, 140, 248, 217],
      widthUnits: 'pixels',
      getWidth: 2,
      pickable: false,
    }))
    layers.push(new TextLayer({
      id: 'query-graph-line-labels',
      data: lines.filter((l) => l.label),
      getPosition: (d) => d.mid,
      getText: (d) => d.label,
      getSize: 11,
      sizeUnits: 'pixels',
      sizeMinPixels: 10,
      sizeMaxPixels: 13,
      getColor: [199, 210, 254, 255],
      background: true,
      getBackgroundColor: [15, 23, 42, 210],
      getBorderColor: [99, 102, 241, 255],
      getBorderWidth: 1,
      pickable: false,
    }))
  }

  return layers
}

// ── USLP augmented links (accepted solid / rejected dashed) ─────────────

export function buildAugmentedLinkLayers({
  accepted = [],
  rejected = [],
  showAcceptedLinks = true,
  showRejectedLinks = true,
  visibleRelations = null,
}) {
  const layers = []
  const visibleSet = visibleRelations ? new Set(visibleRelations) : null
  const isVisible = (relation) => !visibleSet || visibleSet.has(relation)

  const linkRows = (links, kind) => links
    .filter((link) => isVisible(link.relation))
    .map((link) => {
      const hLat = link.head?.lat
      const hLon = link.head?.lon
      const tLat = link.tail?.lat
      const tLon = link.tail?.lon
      if (hLat == null || hLon == null || tLat == null || tLon == null) return null
      const color = hexToRgb(getRelationColor(link.relation))
      return {
        path: [[hLon, hLat], [tLon, tLat]],
        color,
        relation: link.relation,
        normalized_score: link.normalized_score,
        kind,
        head: { ...(link.head || {}), kind },
        tail: { ...(link.tail || {}), kind },
      }
    })
    .filter(Boolean)

  const acceptedRows = showAcceptedLinks ? linkRows(accepted, 'accepted') : []
  if (acceptedRows.length) {
    layers.push(new LineLayer({
      id: 'uslp-accepted-links',
      data: acceptedRows,
      getSourcePosition: (d) => d.path[0],
      getTargetPosition: (d) => d.path[1],
      getColor: (d) => [...d.color, 153],
      widthUnits: 'pixels',
      getWidth: 1.5,
      pickable: false,
    }))
  }

  const rejectedRows = showRejectedLinks ? linkRows(rejected, 'rejected') : []
  if (rejectedRows.length) {
    layers.push(new PathLayer({
      id: 'uslp-rejected-links',
      data: rejectedRows,
      getPath: (d) => d.path,
      getColor: (d) => [...d.color, 102],
      widthUnits: 'pixels',
      getWidth: 1.5,
      pickable: false,
      extensions: [new PathStyleExtension({ dash: true })],
      getDashArray: [4, 3],
    }))
  }

  // Head/tail endpoint markers (accepted filled, rejected hollow). The
  // `info` payload is what the tooltip/EntityInfoPanel read; name falls
  // back to "Head/Tail osm_type/osm_id" since USLP endpoints have no name.
  const endpointRows = []
  for (const row of [...acceptedRows, ...rejectedRows]) {
    const filled = row.kind === 'accepted'
    endpointRows.push({
      position: row.path[0],
      color: row.color,
      filled,
      info: {
        ...row.head,
        name: row.head?.osm_id != null
          ? `Head ${row.head.osm_type || 'osm'}/${row.head.osm_id}`
          : 'Head',
        relation: row.relation,
        normalized_score: row.normalized_score,
      },
    })
    endpointRows.push({
      position: row.path[1],
      color: row.color,
      filled,
      info: {
        ...row.tail,
        name: row.tail?.osm_id != null
          ? `Tail ${row.tail.osm_type || 'osm'}/${row.tail.osm_id}`
          : 'Tail',
        relation: row.relation,
        normalized_score: row.normalized_score,
      },
    })
  }
  if (endpointRows.length) {
    layers.push(new ScatterplotLayer({
      id: 'uslp-endpoints',
      data: endpointRows,
      getPosition: (d) => d.position,
      getRadius: 12,
      radiusUnits: 'pixels',
      filled: true,
      stroked: true,
      getFillColor: (d) => (d.filled ? [...d.color, 217] : [0, 0, 0, 0]),
      getLineColor: (d) => d.color,
      lineWidthMinPixels: 1,
      pickable: true,
      autoHighlight: true,
    }))
  }

  return layers
}

// ── Agent overlays (MCP renderToolOverlay → overlayStore) ───────────────

export function buildAgentOverlayLayers(overlays) {
  const markerRows = []
  const radiusRows = []
  const lineRows = []

  for (const overlay of overlays || []) {
    const color = hexToRgb(overlay.color || TOOL_COLORS[overlay.tool] || '#6366f1')
    const kind = overlay.kind || 'markers'
    const entities = normalizeOverlayEntities(overlay)
    const anchor = overlay.anchor || null

    for (const e of entities) {
      markerRows.push({
        ...e,
        position: [e.lon, e.lat],
        color,
        kind,
        score: e.score ?? 0.5,
      })
    }
    if (kind === 'radius' && anchor && overlay.radius != null) {
      radiusRows.push({
        polygon: buildCirclePolygon(anchor.lon, anchor.lat, overlay.radius),
        color,
      })
    }
    if (kind === 'markers-line' && anchor) {
      for (const e of entities) {
        lineRows.push({ path: [[anchor.lon, anchor.lat], [e.lon, e.lat]], color })
      }
    }
  }

  const layers = []
  if (markerRows.length) {
    layers.push(new ScatterplotLayer({
      id: 'agent-overlay-markers',
      data: markerRows,
      getPosition: (d) => d.position,
      getRadius: (d) => (
        d.kind === 'scaled-markers'
          ? 12 + Math.round(Math.min(Math.max(d.score, 0), 1) * 36)
          : 18
      ),
      radiusUnits: 'pixels',
      filled: true,
      stroked: true,
      getFillColor: (d) => d.color,
      getLineColor: (d) => d.color,
      lineWidthMinPixels: 1,
      pickable: true,
      autoHighlight: true,
    }))
  }
  if (radiusRows.length) {
    layers.push(new PolygonLayer({
      id: 'agent-overlay-radius',
      data: radiusRows,
      getPolygon: (d) => d.polygon,
      filled: true,
      stroked: true,
      getFillColor: (d) => [...d.color, 30],
      getLineColor: (d) => [...d.color, 180],
      lineWidthUnits: 'pixels',
      lineWidthMinPixels: 2,
      pickable: false,
    }))
  }
  if (lineRows.length) {
    layers.push(new LineLayer({
      id: 'agent-overlay-lines',
      data: lineRows,
      getSourcePosition: (d) => d.path[0],
      getTargetPosition: (d) => d.path[1],
      getColor: (d) => [...d.color, 180],
      widthUnits: 'pixels',
      getWidth: 2,
      pickable: false,
    }))
  }

  return layers
}
