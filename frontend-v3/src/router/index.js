import { createRouter, createWebHistory } from 'vue-router'
import HomeV2 from '../views/HomeV2.vue'
import GeoSemanticSurface from '../views/GeoSemanticSurface.vue'
import DoppelgangerSurface from '../views/DoppelgangerSurface.vue'

// URL-gated surfaces (2026-10-15, ISSUE-46): one public path demos one
// feature. The country is a route param so a shared link reproduces
// the exact demo state; without :cc the surface shows the country
// picker (no intermediate index page).
const routes = [
  {
    path: '/',
    name: 'home',
    component: HomeV2,
  },
  {
    path: '/tools/geo-semantic/:cc?',
    name: 'geo-semantic',
    component: GeoSemanticSurface,
    props: true,
  },
  {
    path: '/agents/doppelganger/:cc?',
    name: 'doppelganger',
    component: DoppelgangerSurface,
    props: true,
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
