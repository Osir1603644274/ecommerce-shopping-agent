import { test, expect } from '@playwright/test'

test('independent simulator preserves retry identity and displays receipt separately from application', async ({ page }, testInfo) => {
  test.skip(!process.env.SUPPORT_CONSOLE_URL, 'Requires the separate console test process')
  const requests: { action: string; key: string; body: object }[] = []
  await page.route(`${process.env.SUPPORT_CONSOLE_URL}/action`, async route => {
    const body = route.request().postDataJSON(); requests.push(body)
    if (requests.length === 1) return route.abort('failed')
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: { id: 'receipt-1', status: 'PENDING', event: 'APPROVE' } }) })
  })
  await page.goto(process.env.SUPPORT_CONSOLE_URL!)
  await page.getByRole('combobox', { name: '操作', exact: true }).selectOption('receipt')
  await page.getByLabel('目标编号', { exact: false }).fill('case-1')
  await page.getByRole('textbox', { name: '操作参数', exact: true }).fill(JSON.stringify({ event: 'APPROVE', expectedVersion: 3, reason: '独立模拟审核' }))
  await page.getByRole('button', { name: '执行管理员操作' }).click()
  await expect(page.getByRole('status')).toContainText('本次结果未确认')
  await page.getByRole('button', { name: '执行管理员操作' }).click()
  await expect(page.locator('#result')).toContainText('PENDING')
  expect(requests[0]).toEqual(requests[1])
  await page.getByRole('combobox', { name: '操作', exact: true }).selectOption('receipt-apply')
  await page.getByLabel('目标编号', { exact: false }).fill('receipt-1')
  expect(await page.getByLabel('本次请求幂等键').inputValue()).not.toBe(requests[0].key)
  await page.screenshot({ path: testInfo.outputPath('console-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('console-mobile.png'), fullPage: true })
})
