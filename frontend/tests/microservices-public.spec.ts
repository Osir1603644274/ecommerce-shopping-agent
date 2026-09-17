import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('selected microservices release preserves public shopping and order UI', async ({ page, context }) => {
  test.skip(process.env.RUN_MICROSERVICES_PUBLIC_ACCEPTANCE !== '1', 'Explicit selected-release acceptance only')
  test.setTimeout(90000)
  expect(process.env.PW_BASE_URL).toBe('http://127.0.0.1:5173')
  const root = 'D:/agent-experiments/microservices-release-20260915-attempt001'
  const latest = JSON.parse(readFileSync(`${root}/PUBLIC-MICROSERVICES-LATEST.json`, 'utf8'))
  const saved = JSON.parse(readFileSync(`${root}/${latest.session}`, 'utf8'))
  const acceptance = JSON.parse(readFileSync(`${root}/${latest.artifact}`, 'utf8'))
  expect(saved.account.username).toMatch(/^micro-live-/)
  await context.addCookies(Object.entries(saved.cookies).map(([name, value]) => ({
    name, value: String(value), url: process.env.PW_BASE_URL!, httpOnly: true, sameSite: 'Lax' as const,
  })))
  await page.goto('/#guide')
  const row = page.getByRole('table', { name: '本轮商品推荐' }).locator('tr[data-product-id="4000000005633437"]')
  await expect(row).toBeVisible({ timeout: 30000 })
  await row.getByRole('button', { name: /查看商品|已选择/ }).click()
  await page.getByRole('button', { name: '预览订单', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认下单', exact: true })).toBeEnabled()
  // Do not create another order: the HTTP acceptance already exercised explicit confirmations.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${root}/microservices-public-desktop.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: `${root}/microservices-public-mobile.png`, fullPage: true })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '我的订单', exact: true }).click()
  const order = page.getByRole('article', { name: `订单 ${acceptance.steps.order.orderNo}`, exact: true })
  await expect(order).toBeVisible({ timeout: 30000 })
  await order.getByRole('button', { name: /订单详情/ }).click()
  await expect(page.getByRole('dialog', { name: '订单详情' })).toBeVisible()
  await page.screenshot({ path: `${root}/microservices-public-order.png`, fullPage: true })
})
