import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import VueMcpPlugin from './src/mcp/index.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    VueMcpPlugin(),
  ],
})
