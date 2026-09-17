import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.COMMERCE_BFF_URL || 'http://127.0.0.1:8000'
  const proxy = {
    // Preserve browser Host and Origin together; keep the BFF same-origin guard.
    '/api/commerce-demo': { target, changeOrigin: false },
    '/commerce-demo': { target, changeOrigin: false },
    '/agent/chat-llm': { target, changeOrigin: false },
    '/transaction-agent': { target, changeOrigin: false },
    '/agent-flow': { target, changeOrigin: false },
  }
  return {
    cacheDir: process.env.VITE_CACHE_DIR || 'node_modules/.vite',
    plugins: [react()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy,
      watch: { ignored: ['**/test-results/**', '**/playwright-report/**'] } },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
  }
})
