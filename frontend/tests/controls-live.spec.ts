import { test, expect } from '@playwright/test'

test('real checkpoints: pause, reload, continue and bound knowledge sources', async ({ page }) => {
  test.skip(process.env.RUN_LIVE_CONTROLS !== '1')
  test.setTimeout(240000)
  await page.goto('/')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled()
  await page.getByLabel('告诉我你想找什么').fill('推荐续航好的二手手机，请给出实测依据和资料来源。')
  await page.getByRole('button', { name: '发送消息' }).click()
  const panel = page.getByRole('region', { name: '本轮执行控制' })
  await expect(panel).toContainText('正在执行')
  // Pause can be requested only once the original runner establishes its cursor.
  for (let i = 0; i < 20; i++) {
    if (!(await panel.getByRole('button', { name: '暂停', exact: true }).count())) break
    await panel.getByRole('button', { name: '暂停', exact: true }).click()
    await page.waitForTimeout(400)
    if ((await panel.textContent())?.includes('正在暂停') || (await panel.textContent())?.includes('已暂停')) break
  }
  await expect(panel).toContainText('已暂停', { timeout: 90000 })
  await page.reload()
  await expect(panel).toContainText('已暂停')
  await expect(page.getByLabel('告诉我你想找什么')).toBeDisabled()
  await panel.getByRole('button', { name: '从原检查点继续' }).click()
  await expect(panel).toContainText('已完成', { timeout: 150000 })
  await expect(page.locator('.shop-product').first()).toBeVisible()
  await page.locator('.chat-message.assistant').last().evaluate(el => el.scrollIntoView({block:'start'}))
  await page.screenshot({ path: 'test-results/controls-answer-desktop.png', fullPage: true })
  await panel.locator('summary').click()
  await expect(panel).toContainText('知识库 MCP')
  await panel.getByRole('button', { name: /知识库 MCP/ }).click()
  await expect(panel.getByRole('region', { name: '节点详情' })).toContainText('MCP_STREAMABLE_HTTP')
  await page.screenshot({ path: 'test-results/controls-mcp-flow.png', fullPage: true })
  const evidence = page.locator('.product-evidence').first()
  await evidence.locator('summary').click()
  await expect(evidence.getByRole('link', { name: '查看资料来源 ↗' }).first()).toHaveAttribute('href', /^https?:\/\//)
  await page.screenshot({ path: 'test-results/controls-evidence-desktop.png', fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/controls-evidence-mobile.png', fullPage: true })
})

test('real old phase debugger: explicit next step survives reload', async ({ page }) => {
  test.skip(process.env.RUN_LIVE_CONTROLS !== '1')
  test.setTimeout(240000)
  await page.goto('/')
  await page.getByLabel('单步调试', { exact: true }).check()
  await page.getByLabel('告诉我你想找什么').fill('推荐2000元以内的二手手机')
  await page.getByRole('button', { name: '发送消息' }).click()
  const panel = page.getByRole('region', { name: '本轮执行控制' })
  await expect(panel).toContainText('等待下一步')
  await panel.getByRole('button', { name: '下一步', exact: true }).click()
  await expect(panel).toContainText('理解需求', { timeout: 60000 })
  await page.reload()
  await expect(panel).toContainText('等待下一步')
  for (let i = 0; i < 18; i++) {
    if ((await panel.textContent())?.includes('已完成')) break
    const posted = page.waitForResponse(r => r.url().endsWith('/control/step') && r.request().method() === 'POST')
    await panel.getByRole('button', { name: '下一步', exact: true }).click()
    expect((await posted).ok()).toBe(true)
    await expect(panel.locator('.run-heading strong')).not.toHaveText('正在执行', { timeout: 90000 })
  }
  await expect(panel).toContainText('已完成')
  await expect(page.locator('.shop-product').first()).toBeVisible()
  await page.screenshot({ path: 'test-results/controls-step.png', fullPage: true })
})
