import { test, expect } from '@playwright/test'

for (const mobile of [false, true]) test(`backend request detail and explicit purchase (${mobile ? 'mobile' : 'desktop'})`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 })
  let writes = 0, observed = 0
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/commerce-demo', '')
    const response = (data: unknown, status = 200, headers = {}) => route.fulfill({ status, headers, contentType: 'application/json', body: JSON.stringify(data) })
    if (path === '/me') return response({ authenticated: true, username: 'observer-test', csrfToken: 'test-csrf' })
    if (path === '/capability') return response({ enabled: true, paymentSimulationEnabled: false })
    if (path === '/workspace') return response({ messages: [], cards: [], selection: null, checkout: null, csrfToken: 'test-csrf' })
    if (path === '/workspace/favorites') return response({ products: [] })
    if (path === '/workspace/benefits') {
      if (request.headers()['x-backend-observe'] === '1') observed++
      return response({ coupons: [], campaigns: [{ id: '11', title: '测试二手手机活动', status: 'ACTIVE', salePriceMinor: 9900, availableStock: 3 }], purchases: [] }, 200, { 'X-Backend-Trace-Ticket': 'mock-ticket' })
    }
    if (path === '/backend-traces/mock-ticket') return response({ scope: '测试夹具：不是生产执行证据。', calls: [{ method: 'GET', path: '/api/flash-sales', status: 200, traceId: 'test', detail: {
      droppedEvents: 0, events: [{ kind: 'sql', name: 'FlashSaleMapper.findCampaigns [SELECT]', outcome: 'executed', durationMs: 1.2, affectedRows: 1,
        id: 'sql-event-fixture', parentId: 'method-event-fixture',
        startedAt: '2026-09-10T01:02:03.123Z', finishedAt: '2026-09-10T01:02:03.124Z',
        input: { sql: 'SELECT id FROM campaign WHERE id = ?', bindings: { id: '11' } },
        output: { count: 1 }, source: { file: 'backend/src/main/java/example/FlashSaleMapper.java', line: 42,
          symbol: 'FlashSaleMapper.findCampaigns', snippet: '42: List<Campaign> findCampaigns();',
          notice: '测试夹具，不是实际运行代码证据。' } },
        { kind: 'transaction', name: '只读事务', outcome: 'committed', durationMs: 2, affectedRows: null }],
    } }] })
    if (path === '/workspace/benefits/11/purchase') {
      writes++
      expect(request.postDataJSON()).toEqual({ confirmation: '确认参加抢购' })
      expect(request.headers()['x-csrf-token']).toBe('test-csrf')
      return response({ orderId: 'mock-order', status: 'QUEUED' })
    }
    return response({ detail: 'mock endpoint not provided' }, 404)
  })
  await page.goto('/')
  await page.getByRole('checkbox', { name: '记录 Java 后端执行详情' }).check()
  await page.getByRole('button', { name: '优惠与抢购', exact: true }).click()
  await expect(page.getByText('测试二手手机活动', { exact: true })).toBeVisible()
  expect(writes).toBe(0)
  await page.locator('.backend-request-list button').filter({ hasText: '已收到回复' }).first().click()
  await expect(page.getByText('观测到 1 次 SQL 调用')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /读取抢购活动/ })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认事务结果', exact: true })).toBeVisible()
  await page.getByRole('button', { name: /读取抢购活动/ }).click()
  await page.getByText('查看这一步的代码和参数', { exact: true }).click()
  await expect(page.getByText('SELECT id FROM campaign WHERE id = ?', { exact: false })).toBeVisible()
  await expect(page.getByText('method-event-fixture', { exact: true })).toHaveCount(0)
  await expect(page.locator('[title="2026-09-10T01:02:03.123Z"]')).toHaveCount(0)
  await expect(page.getByText('42: List<Campaign> findCampaigns();', { exact: true })).toBeVisible()
  expect(observed).toBeGreaterThan(0)
  await page.getByRole('button', { name: '参加抢购', exact: true }).click()
  expect(writes).toBe(0)
  await page.getByRole('button', { name: '确认参加抢购', exact: true }).click()
  await expect(page.getByText(/排队落单，受理单号 mock-order/)).toBeVisible()
  expect(writes).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
  await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); window.scrollTo(0, 0) })
  await page.screenshot({ path: `test-results/backend-observer-${mobile ? 'mobile' : 'desktop'}.png`, fullPage: true })
})
