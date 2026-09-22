import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'

test.use({ trace: 'off', video: 'off' })
test('real independent console separates receipt creation and application', async ({ page }, info) => {
  test.skip(!process.env.SUPPORT_CONSOLE_FIXTURE, 'Requires private isolated console fixture')
  const fixture = JSON.parse(readFileSync(process.env.SUPPORT_CONSOLE_FIXTURE!, 'utf8'))
  await page.goto('http://127.0.0.1:19093')
  expect((await page.content()).includes(fixture.adminToken)).toBe(false)
  const observations: object[] = []
  async function action(name: string, id: string, body = {}) {
    await page.getByRole('combobox', { name: '操作', exact: true }).selectOption(name)
    await page.getByLabel('目标编号', { exact: false }).fill(id)
    await page.getByRole('textbox', { name: '操作参数', exact: true }).fill(JSON.stringify(body))
    const responsePromise = page.waitForResponse(r => r.url().endsWith('/action') && r.request().method() === 'POST')
    await page.getByRole('button', { name: '执行管理员操作' }).click()
    const response = await responsePromise; expect(response.ok()).toBeTruthy()
    const value = (await response.json()).data
    await expect(page.getByRole('status')).toContainText('已取得权威返回')
    observations.push({ action: name, value });return value
  }
  const initial = await action('case-state', fixture.caseId)
  expect(initial.case.phase).toBe('AWAITING_REVIEW')
  const approval = await action('receipt', fixture.caseId, { event: 'APPROVE', expectedVersion: initial.case.version, reason: '独立控制台页面审核' })
  expect(approval.status).toBe('PENDING')
  expect((await action('case-state', fixture.caseId)).case.phase).toBe('AWAITING_REVIEW')
  await action('receipt-apply', approval.id)
  expect((await action('case-state', fixture.caseId)).case.phase).toBe('REFUND_PENDING')
  const refund = await action('refund-success', fixture.caseId)
  expect(refund.status).toBe('PENDING')
  expect((await action('case-state', fixture.caseId)).case.phase).toBe('REFUND_PENDING')
  await action('receipt-apply', refund.id)
  expect((await action('case-state', fixture.caseId)).case.phase).toBe('COMPLETED')
  writeFileSync(info.outputPath('observations.json'), JSON.stringify(observations, null, 2))
  await page.screenshot({ path: info.outputPath('console-live-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('console-live-mobile.png'), fullPage: true })
})
