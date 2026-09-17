import { test, expect } from '@playwright/test'
import { randomBytes } from 'node:crypto'

test('live workspace ownership, stock rejection, stale tab and mobile favorites', async ({ page, browser }) => {
  test.skip(process.env.RUN_LIVE_SHOP !== '1', 'Local isolated accounts and favorites; no new orders or payments.')
  test.setTimeout(90_000)
  const origin = process.env.PW_BASE_URL || 'http://127.0.0.1:5180'
  const root = '/api/commerce-demo'
  const password = randomBytes(20).toString('hex')
  const owner = `workspace-safe-a-${Date.now()}`
  const other = `workspace-safe-b-${Date.now()}`
  const registered = await page.request.post(root + '/register', { headers: { Origin: origin }, data: { username: owner, password } })
  expect(registered.ok()).toBe(true)
  const aHeaders = { Origin: origin, 'X-CSRF-Token': (await registered.json()).csrfToken }
  expect((await page.request.get(root + '/workspace', { headers: aHeaders })).ok()).toBe(true)
  expect((await page.request.put(root + '/workspace/favorites/2278548', { headers: aHeaders })).ok()).toBe(true)
  expect((await page.request.post(root + '/workspace/selection', { headers: aHeaders, data: { productId: 2278548 } })).ok()).toBe(true)
  const tooMany = await page.request.post(root + '/workspace/preview', { headers: aHeaders, data: { productId: 2278548, quantity: 20 } })
  expect(tooMany.ok()).toBe(false)
  expect(await tooMany.text()).toContain('库存')
  const preview = await page.request.post(root + '/workspace/preview', { headers: aHeaders, data: { productId: 2278548, quantity: 1 } })
  expect(preview.ok()).toBe(true)
  const confirmationId = (await preview.json()).checkout.proposal.confirmationId
  const bContext = await browser.newContext({ baseURL: origin })
  try {
    const registeredB = await bContext.request.post(root + '/register', { headers: { Origin: origin }, data: { username: other, password } })
    expect(registeredB.ok()).toBe(true)
    const bHeaders = { Origin: origin, 'X-CSRF-Token': (await registeredB.json()).csrfToken }
    expect((await bContext.request.get(root + '/workspace', { headers: bHeaders })).ok()).toBe(true)
    expect((await bContext.request.get(root + '/workspace/favorites', { headers: bHeaders })).ok()).toBe(true)
    expect((await (await bContext.request.get(root + '/workspace/favorites', { headers: bHeaders })).json()).products).toEqual([])
    expect((await bContext.request.post(root + '/workspace/selection', { headers: bHeaders, data: { productId: 2278548 } })).status()).toBe(409)
    expect((await bContext.request.post(root + '/workspace/confirm', { headers: bHeaders, data: { confirmationId } })).status()).toBe(409)
    // A real paid order from the immediately preceding isolated end-to-end acceptance.
    const paidOrder = process.env.LIVE_FOREIGN_ORDER_ID
    const paidPayment = process.env.LIVE_FOREIGN_PAYMENT_ID
    expect(paidOrder).toBeTruthy()
    expect(paidPayment).toBeTruthy()
    for (const response of [
      await bContext.request.get(root + '/orders/' + paidOrder, { headers: bHeaders }),
      await bContext.request.post(root + '/workspace/payment-preview', { headers: bHeaders, data: { orderId: paidOrder } }),
      await bContext.request.post(root + '/payments/' + paidPayment + '/simulate-success', { headers: bHeaders }),
    ]) expect([403, 404]).toContain(response.status())

    await page.goto('/#favorites')
    await expect(page.locator('.shop-product')).toHaveCount(1)
    await page.screenshot({ path: 'test-results/unified-live-favorites.png', fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.screenshot({ path: 'test-results/unified-live-mobile-favorites.png', fullPage: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const secondTab = await page.context().newPage()
    await secondTab.goto('/')
    const switched = await secondTab.request.post(root + '/login', { headers: { Origin: origin }, data: { username: other, password } })
    expect(switched.ok()).toBe(true)
    // The old tab still holds A's CSRF but the shared cookie now belongs to B.
    await page.getByRole('button', { name: /^取消收藏/ }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await expect(page.locator('.shop-product')).toHaveCount(0)
    await expect(page.locator('.account-name')).toHaveCount(0)
    test.info().annotations.push({ type: 'isolated-test-accounts', description: `${owner}, ${other}` })
  } finally {
    await bContext.close()
  }
})
