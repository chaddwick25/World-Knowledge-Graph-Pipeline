/**
 * seedStore — the doppelganger seed's single state owner (ISSUE-48).
 *
 * One concern, one store: the panel writes from `data-seed` stream parts;
 * WorldKGMap and EntityInfoPanel read it and gesture through it (a
 * candidate-marker click IS the confirmation); finalize reads the
 * confirmed row. No component-to-component seed state copies.
 *
 * The confirmed row keeps the flat `_verify_seed` shape
 * ({osm_id, osm_type, name, tags, lat, lon}) so the finalize payload
 * needs no re-shaping.
 */
import { defineStore } from 'pinia'

export const useSeedStore = defineStore('seed', {
  state: () => ({
    asked: '',
    // The confirmed seed (head row of a single hit, or the user's
    // clicked candidate). Null until found; `hasSeed` derives the
    // rest - no separate found flag (state is minimal, derive the
    // remainder).
    confirmed: null,
    // 'resolution' (auto-resolved head row) | 'explicit' (the user's
    // pick: map marker, info-box button, or chat list chip).
    confirmedSource: null,
    // An explicit pick locks the seed: later data-seed parts update
    // candidates/suggestions for display but never clobber it.
    locked: false,
    // Multi-hit rows (distinct marker style + compact chat list).
    // RETAINED after a pick so the menu/map stay live for switching;
    // `confirmed` marks the picked one.
    candidates: [],
    // Not-found nearest names ("did you mean ...?").
    suggestions: [],
    // The hovered candidate's key (osm_type:osm_id) - shared hover
    // state linking the info box and the chat menu (bidirectional).
    hoveredKey: null,
  }),
  getters: {
    hasSeed: (state) => !!(state.confirmed && state.confirmed.lat != null),
    isMultiHit: (state) => (state.candidates || []).length > 1,
  },
  actions: {
    /** Apply a `data-seed` stream part's data payload. A locked seed
     * (the user's explicit pick) is never clobbered. */
    applySeedPart(data) {
      if (!data) return
      this.asked = data.asked || ''
      this.candidates = data.candidates || []
      this.suggestions = data.suggestions || []
      if (this.locked) return
      if (data.found) {
        this.confirmed = {
          osm_id: data.osm_id,
          osm_type: data.osm_type,
          name: data.name,
          tags: data.tags || {},
          lat: data.lat,
          lon: data.lon,
        }
        this.confirmedSource = 'resolution'
      } else {
        this.confirmed = null
        this.confirmedSource = null
      }
    },
    /** The map click / info-box button / list click: the confirmation
     * gesture. Explicit, so it locks. Candidates are RETAINED (the
     * menu and amber markers stay live; clicking another candidate
     * switches the seed). */
    selectCandidate(row) {
      if (!row || row.lat == null) return
      this.confirmed = {
        osm_id: row.osm_id,
        osm_type: row.osm_type,
        name: row.name,
        tags: row.tags || {},
        lat: row.lat,
        lon: row.lon,
      }
      this.confirmedSource = 'explicit'
      this.locked = true
      this.suggestions = []
      // The picked entity's button is about to become the Seed badge;
      // its mouseleave will never fire, so clear the hover here.
      this.hoveredKey = null
    },
    /** Shared hover state: the info box and the chat menu both write
     * and read it, so hovering one highlights the other. */
    setHoveredKey(key) {
      this.hoveredKey = key || null
    },
    clearHoveredKey(key) {
      if (key == null || this.hoveredKey === key) {
        this.hoveredKey = null
      }
    },
    /** The finalize payload: the confirmed row + the original ask. */
    finalizePayload() {
      if (!this.confirmed) return null
      return { ...this.confirmed, asked: this.asked, found: true }
    },
    reset() {
      this.asked = ''
      this.confirmed = null
      this.confirmedSource = null
      this.locked = false
      this.candidates = []
      this.suggestions = []
      this.hoveredKey = null
    },
  },
})
