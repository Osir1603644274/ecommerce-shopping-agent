import { test, expect } from '@playwright/test'

for (const mobile of [false, true]) test(`Agent receipt details (${mobile ? 'mobile' : 'desktop'})`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 })
  const state = { cards: [], selection: null, checkout: null, csrfToken: 'fixture', messages: [
    { role: 'assistant', content: '测试夹具：展示执行回执，不是线上模型运行。', requestId: 'fixture-request', flow: [
      { kind: 'tool', label: 'search_products', outcome: 'tool_succeeded', id: 'task|plan|step', parentId: 'plan',
        startedAt: '2026-09-10T01:02:03.123Z', finishedAt: '2026-09-10T01:02:03.456Z', durationMs: 333,
        cause: { requestId: 'fixture-request', stepId: 'step' }, input: { query: '续航手机', limit: 3 },
        output: { count: 3 }, source: { file: 'agent/app/domains/ecommerce/tools.py', line: 123,
          function: 'search_products_tool', snippet: '123: async def search_products_tool(...):', scope: '测试源码夹具' } },
    ] },
  ] }
  await page.route('**/api/**', r => r.fulfill({ status: r.request().url().endsWith('/me') ? 401 : 200,
    contentType: 'application/json', body: JSON.stringify(state) }))
  await page.goto('/')
  await page.getByText('查看本轮执行记录', { exact: true }).click()
  await page.getByRole('button', { name: /检索商品/ }).click()
  const detail = page.getByRole('region', { name: '节点详情' })
  await expect(detail.getByText('"query": "续航手机"', { exact: false })).toBeVisible()
  await expect(detail.locator('[title="2026-09-10T01:02:03.123Z"]')).toHaveCount(0)
  await expect(detail.getByText('agent/app/domains/ecommerce/tools.py:123', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await page.screenshot({ path: `test-results/execution-detail-${mobile ? 'mobile' : 'desktop'}.png`, fullPage: true })
})
