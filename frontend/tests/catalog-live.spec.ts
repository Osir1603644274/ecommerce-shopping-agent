import { test, expect } from '@playwright/test'

test.use({ storageState: process.env.CATALOG_BROWSER_STATE || undefined })

test('public catalog uses the real shopping page, survives reload and exposes no purchase cards', async ({ page }) => {
  test.skip(process.env.RUN_LIVE_CATALOG !== '1')
  test.setTimeout(360000)
  const errors: string[] = []
  const artifacts = process.env.CATALOG_ARTIFACT_DIR || 'D:/agent-datasets/catalog-main-integration-20260913-v1'
  page.on('pageerror', error => errors.push(error.message))
  await page.setExtraHTTPHeaders({ 'X-Conversation-Source': 'automated_test' })
  await page.goto('/')
  let submittedAt: number | undefined
  if (!process.env.CATALOG_BROWSER_STATE) {
    await page.getByLabel('告诉我你想找什么').fill(process.env.CATALOG_LIVE_QUERY || '找可折叠笔记本电脑支架')
    submittedAt = Date.now()
    await page.getByRole('button', { name: '发送消息' }).click()
  }
  const panel = page.getByRole('region', { name: '本轮执行控制' })
  await expect(panel).toContainText('本轮普通商品搜索已完成', { timeout: 300000 })
  if (submittedAt !== undefined) test.info().annotations.push({ type: 'submitToCompletedSeconds', description: String((Date.now() - submittedAt) / 1000) })
  const answer = page.locator('.chat-message.assistant').last()
  await expect(answer).toContainText('来源记录')
  await expect(answer).toContainText('价格和库存未核实')
  await expect(page.locator('.shop-product')).toHaveCount(0)
  await expect(answer).not.toContainText('source_claim')
  await expect(answer).not.toContainText('verified_price')
  const text = await answer.locator('.message-copy').innerText()
  await page.reload()
  await expect.poll(() => page.locator('.chat-message.assistant .message-copy').last().innerText()).toBe(text)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${artifacts}/catalog-desktop.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${artifacts}/catalog-mobile.png`, fullPage: true })
  expect(errors).toEqual([])
})
