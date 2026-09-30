import { LineLayer, PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'

const EARTH_RADIUS_M = 6371000
const SUPPORTED_TEMPLATES = new Set([
  'GEOCODE-BATCH-COMPARE (#4)',
  'FILTER-AGGREGATE-MEASURE (#1)',
  'PLACE-ATTRIBUTE-QUERY (#8)',
  'LOCATION-BEARING-CLASSIFY (#5)',
  'OBJECT-FIELD-MEASURE (#2)',
])

function toRad(degrees) {
  return (degrees * Math.PI) / 180
}

function formatDistance(m) {
  if (m == null) return ''
  if (m >= 1000) return `${(m / 1000).toFixed(1)} km`
  return `${Math.round(m)} m`
}

function toDeg(radians) {
  return (radians * 180) / Math.PI
}

function normalizeLng(lng) {
  let value = lng
  while (value > 180) value -= 360
  while (value < -180) value += 360
  return value
}

export function destinationPoint(lat, lon, bearingDeg, distanceM) {
  const angularDistance = distanceM / EARTH_RADIUS_M
  const bearing = toRad(bearingDeg)
  const lat1 = toRad(lat)
  const lon1 = toRad(lon)
  const sinLat1 = Math.sin(lat1)
  const cosLat1 = Math.cos(lat1)
  const sinAngular = Math.sin(angularDistance)
  const cosAngular = Math.cos(angularDistance)
  const sinLat2 = (sinLat1 * cosAngular) + (cosLat1 * sinAngular * Math.cos(bearing))
  const lat2 = Math.asin(sinLat2)
  const lon2 = lon1 + Math.atan2(
    Math.sin(bearing) * sinAngular * cosLat1,
    cosAngular - (sinLat1 * sinLat2)
  )
  return [normalizeLng(toDeg(lon2)), toDeg(lat2)]
}

export function buildCirclePolygon(lon, lat, radiusM, segments = 64) {
  const ring = Array.from({ length: segments + 1 }, (_, i) => {
    const bearing = (360 * i) / segments
    return destinationPoint(lat, lon, bearing, radiusM)
  })
  return [ring]
}

export function buildConePolygon(lon, lat, bearingDeg, radiusM, halfAngleDeg = 45, segments = 32) {
  const start = bearingDeg - halfAngleDeg
  const step = (halfAngleDeg * 2) / segments
  const arc = Array.from({ length: segments + 1 }, (_, i) => (
    destinationPoint(lat, lon, start + (i * step), radiusM)
  ))
  return [[[lon, lat], ...arc, [lon, lat]]]
}

function buildArcRows(viz) {
  const anchors = viz.anchors || []
  const entities = viz.entities || []
  const links = viz.links || []
  return links
    .map((link) => {
      const anchor = anchors[link.anchorIdx]
      const entity = entities[link.entityIdx]
      if (!anchor || !entity?.geom) return null
      return {
        sourcePosition: [anchor.lon, anchor.lat],
        targetPosition: [entity.geom.lon, entity.geom.lat],
      }
    })
    .filter(Boolean)
}

function buildLineRows(viz) {
  return (viz.anchorLines || [])
    .map((line, index) => {
      const from = line.from
      const to = line.to
      if (!from || !to) return null
      return {
        id: `line-${index}`,
        path: [[from.lon, from.lat], [to.lon, to.lat]],
        label: line.label || '',
        mid: [(from.lon + to.lon) / 2, (from.lat + to.lat) / 2],
      }
    })
    .filter(Boolean)
}

function parseDirectionBearing(direction) {
  const values = {
    north: 0, n: 0,
    northeast: 45, ne: 45,
    east: 90, e: 90,
    southeast: 135, se: 135,
    south: 180, s: 180,
    southwest: 225, sw: 225,
    west: 270, w: 270,
    northwest: 315, nw: 315,
  }
  if (!direction) return null
  return values[String(direction).toLowerCase()] ?? null
}

export function isTemplateDeckSupported(templateName) {
  return SUPPORTED_TEMPLATES.has(templateName)
}

export function buildTemplateDeckLayers(viz) {
  if (!viz || !viz.template || !isTemplateDeckSupported(viz.template)) return []

  const anchors = (viz.anchors || [])
    .filter((a) => a && a.lat != null && a.lon != null)
    .map((a, i) => ({ ...a, id: `anchor-${i}` }))
  const entities = (viz.entities || [])
    .filter((e) => e?.geom && e.geom.lat != null && e.geom.lon != null)
    .map((e, i) => ({ ...e, _id: `entity-${i}` }))
  const layers = []

  if (anchors.length) {
    layers.push(new ScatterplotLayer({
      id: 'template-anchor-points',
      data: anchors,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: 255,
      radiusUnits: 'meters',
      radiusMinPixels: 18,
      radiusMaxPixels: 42,
      filled: true,
      stroked: true,
      // Anchors are orange (flipped from indigo 2026-09-28)
      getFillColor: [249, 115, 22, 220],
      getLineColor: [255, 237, 213, 255],
      lineWidthMinPixels: 2,
      pickable: true,
      autoHighlight: true,
    }))
  }

  if (entities.length) {
    layers.push(new ScatterplotLayer({
      id: 'template-entity-points',
      data: entities,
      getPosition: (d) => [d.geom.lon, d.geom.lat],
      getRadius: (d) => d.distance_m != null ? 165 : 210,
      radiusUnits: 'meters',
      radiusMinPixels: 12,
      radiusMaxPixels: 30,
      filled: true,
      stroked: true,
      // Entities are indigo/purple (flipped from orange 2026-09-28)
      getFillColor: [99, 102, 241, 210],
      getLineColor: [224, 231, 255, 255],
      lineWidthMinPixels: 1,
      pickable: true,
      autoHighlight: true,
    }))
    // Per-entity name + distance labels (same TextLayer pattern as the
    // map labels and the distance-line labels).
    layers.push(new TextLayer({
      id: 'template-entity-labels',
      data: entities.filter((e) => e.name),
      getPosition: (d) => [d.geom.lon, d.geom.lat],
      getText: (d) => `${d.name}${d.distance_m != null ? ` · ${formatDistance(d.distance_m)}` : ''}`,
      getSize: 13,
      sizeUnits: 'pixels',
      sizeMinPixels: 11,
      sizeMaxPixels: 15,
      padding: 4,
      getColor: [226, 232, 240, 230],
      getPixelOffset: [8, -8],
      getTextAnchor: 'start',
      getAlignmentBaseline: 'bottom',
      background: true,
      getBackgroundColor: [15, 23, 42, 160],
      getBorderColor: [99, 102, 241, 120],
      getBorderWidth: 1,
      pickable: false,
    }))
  }

  const arcs = buildArcRows(viz)
  if (arcs.length) {
    // Straight anchor→entity links (2026-09-28 — the curved ArcLayer edges
    // were replaced with straight LineLayer rows; color family unchanged).
    layers.push(new LineLayer({
      id: 'template-anchor-links',
      data: arcs,
      getSourcePosition: (d) => d.sourcePosition,
      getTargetPosition: (d) => d.targetPosition,
      getColor: [165, 180, 252, 220],
      widthUnits: 'pixels',
      getWidth: 2,
      widthMinPixels: 1.5,
      widthMaxPixels: 3,
      pickable: false,
    }))
  }

  const lines = buildLineRows(viz)
  if (lines.length) {
    layers.push(new PathLayer({
      id: 'template-distance-lines',
      data: lines,
      getPath: (d) => d.path,
      widthUnits: 'pixels',
      getWidth: 2.5,
      getColor: [129, 140, 248, 230],
      pickable: false,
    }))
    layers.push(new TextLayer({
      id: 'template-distance-labels',
      data: lines.filter((d) => d.label),
      getPosition: (d) => d.mid,
      getText: (d) => d.label,
      getSize: 12,
      sizeUnits: 'pixels',
      sizeMinPixels: 10,
      sizeMaxPixels: 14,
      getColor: [199, 210, 254, 255],
      background: true,
      getBackgroundColor: [15, 23, 42, 210],
      getBorderColor: [99, 102, 241, 255],
      getBorderWidth: 1,
      pickable: false,
    }))
  }

  if (viz.radiusM && anchors.length) {
    const anchor = anchors[0]
    layers.push(new PolygonLayer({
      id: 'template-radius-ring',
      data: [{
        polygon: buildCirclePolygon(anchor.lon, anchor.lat, viz.radiusM),
      }],
      getPolygon: (d) => d.polygon,
      filled: true,
      stroked: true,
      getFillColor: [56, 189, 248, 28],
      getLineColor: [56, 189, 248, 170],
      lineWidthUnits: 'pixels',
      lineWidthMinPixels: 2,
      pickable: false,
    }))
  }

  if (viz.coneRadiusM && anchors.length) {
    const anchor = anchors[0]
    const bearing = parseDirectionBearing(viz.direction)
    if (bearing != null) {
      layers.push(new PolygonLayer({
        id: 'template-direction-cone',
        data: [{
          polygon: buildConePolygon(anchor.lon, anchor.lat, bearing, viz.coneRadiusM),
        }],
        getPolygon: (d) => d.polygon,
        filled: true,
        stroked: true,
        getFillColor: [16, 185, 129, 40],
        getLineColor: [16, 185, 129, 180],
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 2,
        pickable: false,
      }))
    }
  }

  return layers
}

export function templateVizBounds(viz) {
  if (!viz) return []
  const anchorBounds = (viz.anchors || [])
    .filter((a) => a && a.lat != null && a.lon != null)
    .map((a) => [a.lat, a.lon])
  const entityBounds = (viz.entities || [])
    .filter((e) => e?.geom && e.geom.lat != null && e.geom.lon != null)
    .map((e) => [e.geom.lat, e.geom.lon])
  return [...anchorBounds, ...entityBounds]
}
