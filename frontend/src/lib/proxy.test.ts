import { describe, expect, it } from 'vitest'
import viteConfig from '../../vite.config'

describe('same-origin development integration', () => {
  it('keeps Host/Origin paired for orders and every legacy shopping API', async () => {
    const config = typeof viteConfig === 'function'
      ? await viteConfig({ command: 'serve', mode: 'test' }) : await viteConfig
    for (const path of ['/api/commerce-demo', '/commerce-demo', '/agent/chat-llm', '/transaction-agent', '/agent-flow']) {
      expect(config.server?.proxy?.[path]).toMatchObject({ changeOrigin: false })
      expect(config.preview?.proxy?.[path]).toMatchObject({ changeOrigin: false })
    }
  })
})
