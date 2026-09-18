import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import VueMcpPlugin from './src/mcp/index.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    VueMcpPlugin(),
  ],
  build: {
    rollupOptions: {
      output: {
        // deck.gl is a +1 MB dependency used only by the Deck GL tab —
        // split it out of the main chunk so the app shell stays lean
        // (DECKGL_VISUALIZATION_INTEGRATION_PLAN.md §9 bundle-size risk).
        manualChunks: {
          deckgl: [
            '@deck.gl/core',
            '@deck.gl/layers',
            '@deck.gl/extensions',
            '@deck.gl-community/leaflet',
          ],
        },
      },
    },
  },
})
