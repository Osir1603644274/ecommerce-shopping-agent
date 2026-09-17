import { test, expect } from '@playwright/test'
import { randomBytes } from 'node:crypto'

test('merged real cart: cancellation, partial refund confirmation, fulfillment, ownership', async ({ page, browser }) => {
  test.skip(process.env.RUN_LIVE_SHOP !== '1', 'Opt-in local transactions; isolated account; no real money')
  test.setTimeout(180000)
  const root = '/api/commerce-demo', origin = process.env.PW_BASE_URL!
  const account = `merge-qa-${Date.now()}`, password = randomBytes(20).toString('hex')
  const registration = await page.request.post(root + '/register', { headers: { Origin: origin }, data: { username: account, password } })
  expect(registration.ok()).toBeTruthy()
  const headers = { Origin: origin, 'X-CSRF-Token': (await registration.json()).csrfToken }
  async function post(path: string, data: object = {}) {
    const r = await page.request.post(root + '/workspace' + path, { headers, data })
    expect(r.ok(), await r.text()).toBeTruthy(); return r.json()
  }
  await page.request.get(root + '/workspace', { headers })
  expect((await page.request.put(root + '/workspace/favorites/30148', { headers })).ok()).toBeTruthy()
  async function create(quantity: number) {
    await post('/selection', { productId: 30148 })
    const quote = await post('/preview', { productId: 30148, quantity })
    const id = quote.checkout.proposal.confirmationId
    const receipt = await post('/confirm', { confirmationId: id })
    expect(receipt.checkout.outcome.result.status).toBe('PENDING_PAYMENT')
    const replay = await post('/confirm', { confirmationId: id })
    expect(replay.checkout.outcome.result.id).toBe(receipt.checkout.outcome.result.id)
    return receipt.checkout.outcome.result.id as string
  }
  const cancelled = await create(1)
  const cancelQuote = await post('/cancel-preview', { orderId: cancelled })
  expect((await post('/confirm', { confirmationId: cancelQuote.checkout.proposal.confirmationId })).checkout.outcome.result.status).toBe('CANCELLED')
  const paid = await create(2)
  const paymentQuote = await post('/payment-preview', { orderId: paid })
  const payment = await post('/confirm', { confirmationId: paymentQuote.checkout.proposal.confirmationId })
  const payId = payment.checkout.outcome.result.id
  const simulated = await page.request.post(root + `/payments/${payId}/simulate-success`, { headers })
  expect(simulated.ok(), await simulated.text()).toBeTruthy()
  // Read by another real principal must not expose state or permit a refund preview.
  const other = await browser.newContext({ baseURL: origin })
  try {
    const r = await other.request.post(root + '/register', { headers: { Origin: origin }, data: { username: account + '-b', password } })
    const h = { Origin: origin, 'X-CSRF-Token': (await r.json()).csrfToken }
    await other.request.get(root + '/workspace', { headers: h })
    expect([403, 404]).toContain((await other.request.get(root + `/workspace/orders/${paid}/after-sales`, { headers: h })).status())
    expect((await other.request.post(root + '/workspace/refund-preview', { headers: h,
      data: { orderId: paid, items: [{ itemId: 30148, quantity: 1 }], reason: 'not mine' } })).ok()).toBe(false)
  } finally { await other.close() }
  await page.goto('/#orders')
  const card = page.locator('.order-card').filter({ has: page.locator('.status-badge', { hasText: '已支付' }) })
  await card.getByRole('button', { name: /详情/ }).click()
  await expect(page.getByText('待出库', { exact: true })).toBeVisible({ timeout: 25000 })
  await page.getByLabel('退款数量', { exact: true }).fill('1')
  await page.getByRole('button', { name: '预览退款金额' }).click()
  await expect(page.getByRole('button', { name: '确认申请退款', exact: true })).toBeEnabled()
  await expect(page.getByRole('region', { name: '交易确认卡' })).toContainText('× 1')
  await page.screenshot({ path: 'test-results/merged-refund-confirmation.png', fullPage: true })
  await page.getByRole('button', { name: '确认申请退款', exact: true }).click()
  await expect(page.getByText('退款申请已创建', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '本地模拟退款成功' }).click()
  await page.locator('.order-card').filter({ has: page.locator('.status-badge', { hasText: '已支付' }) })
    .getByRole('button', { name: /详情/ }).click()
  await expect(page.getByText(/已退 1 件/)).toBeVisible()
  await expect(page.getByText('已出库', { exact: true })).toBeVisible({ timeout: 60000 })
  await expect(page.getByText(/模拟运单：SIM-/)).toBeVisible()
  await page.screenshot({ path: 'test-results/merged-fulfillment-refund.png', fullPage: true })
  test.info().annotations.push({ type: 'real-local-receipts', description: JSON.stringify({ account, paid, cancelled, payId }) })
})
