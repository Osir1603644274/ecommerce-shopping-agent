import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('external catalog real server-bound card, preview and responsive page', async ({ page, context }) => {
  test.skip(process.env.RUN_FULL_CATALOG_ACCEPTANCE !== '1', 'Owned acceptance runtime only')
  test.setTimeout(90000)
  expect(process.env.PW_BASE_URL).toBe('http://127.0.0.1:5174')
  const root = 'D:/agent-datasets/commerce-full-release-20260915-attempt001'
  const saved = JSON.parse(readFileSync(`${root}/acceptance-session.private.json`, 'utf8'))
  await context.addCookies(Object.entries(saved.cookies).map(([name, value]) => ({ name, value: String(value), url: process.env.PW_BASE_URL!, httpOnly: true, sameSite: 'Lax' as const })))
  await page.goto('/#guide')
  const card = page.getByRole('table', { name: '本轮商品推荐' }).getByRole('row').filter({ hasText: '香榭丽舍' }).first()
  await expect(card).toBeVisible({ timeout: 30000 })
  await expect(card).toContainText('2,160.00')
  await card.getByRole('button', { name: /查看商品|已选择/ }).click()
  await expect(page.getByRole('button', { name: '预览订单', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '预览订单', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认下单', exact: true })).toBeEnabled()
  await page.screenshot({ path: `${root}/full-catalog-desktop-preview.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${root}/full-catalog-mobile-preview.png`, fullPage: true })
  // No click on confirmation: this UI check must not create another order.
})
