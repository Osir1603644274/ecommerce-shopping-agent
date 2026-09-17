import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('public full-catalog tail product can reach order preview', async ({ page, context }) => {
  test.skip(process.env.RUN_FULL_CATALOG_PUBLIC_ACCEPTANCE !== '1', 'Explicit public release acceptance only')
  test.setTimeout(90000)
  expect(process.env.PW_BASE_URL).toBe('http://127.0.0.1:5173')
  const root = 'D:/agent-datasets/commerce-full-release-20260915-attempt001'
  const saved = JSON.parse(readFileSync(`${root}/live-session.private.json`, 'utf8'))
  expect(saved.username).toMatch(/^full-live-/)
  await context.addCookies(Object.entries(saved.cookies).map(([name, value]) => ({ name, value: String(value), url: process.env.PW_BASE_URL!, httpOnly: true, sameSite: 'Lax' as const })))
  await page.goto('/#guide')
  const row = page.getByRole('table', { name: '本轮商品推荐' }).locator('tr[data-product-id="4000000005633437"]')
  await expect(row).toBeVisible({ timeout: 30000 })
  await expect(row).toContainText('3,509.00')
  await row.getByRole('button', { name: /查看商品|已选择/ }).click()
  await page.getByRole('button', { name: '预览订单', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认下单', exact: true })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${root}/public-full-catalog-desktop.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${root}/public-full-catalog-mobile.png`, fullPage: true })
  // The HTTP acceptance runner records a single explicit local test order separately.
})
