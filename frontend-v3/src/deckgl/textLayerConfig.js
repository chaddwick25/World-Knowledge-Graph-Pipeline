/**
 * textLayerConfig — factory functions for the two deck.gl TextLayers.
 *
 * DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md §3.2.3. Factory functions only
 * (rule 01-frontend-patterns.md — no config objects with handler functions).
 *
 * Data contract (driven by the backend endpoints, Phase 1b):
 *   classLabels  [{ label, centroid: [lon, lat], count }]   — class-centroids/
 *   entityLabels [{ name, position: [lon, lat], score }]    — entities/
 *
 * Display settings (DeckGlControls, Phase 1c):
 *   limit      — cap on how many labels the layer renders (data.slice)
 *   sizeScale  — multiplies the pixel clamps (and the entity size basis) so
 *                the size slider stays effective at every zoom (with
 *                sizeUnits meters/pixels the clamps would otherwise
 *                dominate and swallow the slider).
 *
 * The zoom-hierarchy layerFilter, 250ms alpha transitions and
 * CollisionFilterExtension land in Phase 1c — these factories stay stable.
 */

import { TextLayer } from '@deck.gl/layers'

export function createClassLabels(classData, { limit = 100, sizeScale = 1 } = {}) {
  return new TextLayer({
    id: 'wkg-class-labels',
    data: classData.slice(0, limit),
    getPosition: (d) => d.centroid,
    getText: (d) => d.label,
    getSize: (d) => d.count,
    // Meters so labels scale with the map: big at low zoom, small at high zoom.
    sizeUnits: 'meters',
    sizeMinPixels: 14 * sizeScale,
    sizeMaxPixels: 48 * sizeScale,
    getColor: [200, 200, 200, 255],
  })
}

export function createEntityLabels(entityData, { limit = 5000, sizeScale = 1 } = {}) {
  return new TextLayer({
    id: 'entity-tag-labels',
    data: entityData.slice(0, limit),
    getPosition: (d) => d.position,
    getText: (d) => d.name,
    getSize: (d) => d.score * sizeScale,
    // Pixels so entity names keep a constant screen size at high zoom.
    sizeUnits: 'pixels',
    sizeMinPixels: 10 * sizeScale,
    sizeMaxPixels: 20 * sizeScale,
    getColor: [120, 180, 255, 255],
  })
}
