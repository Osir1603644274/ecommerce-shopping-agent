import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

test.use({ trace: 'off', video: 'off' })
test('5173 real support preview, confirmation and independent refund receipt', async ({ page }, info) => {
  test.skip(!process.env.SUPPORT_BROWSER_FIXTURE, 'Requires isolated private fixture')
  test.setTimeout(120000)
  page.setDefaultTimeout(15000)
  const fixture = JSON.parse(readFileSync(process.env.SUPPORT_BROWSER_FIXTURE!, 'utf8'))
  const type = process.env.SUPPORT_BROWSER_TYPE || 'REFUND_ONLY'
  const labels: Record<string, string> = { REFUND_ONLY: '仅退款', RETURN_REFUND: '退货退款', EXCHANGE: '同款同规格换货' }
  expect(Object.keys(labels)).toContain(type)
  const label = labels[type]
  expect(process.env.PW_BASE_URL).toBe('http://127.0.0.1:5173')
  const origin = process.env.PW_BASE_URL!, root = '/api/commerce-demo'
  const login = await page.request.post(root + '/login', { headers: { Origin: origin }, data: fixture.account })
  expect(login.ok()).toBeTruthy()
  const headers = { Origin: origin, 'X-CSRF-Token': (await login.json()).csrfToken }
  page.on('request', request => {
    if (request.url().startsWith(origin + root)) {
      const csrf = request.headers()['x-csrf-token']; if (csrf) headers['X-CSRF-Token'] = csrf
    }
  })
  await page.request.get(root + '/workspace', { headers })
  await page.goto('/#orders')
  await page.getByRole('button', { name: '订单详情', exact: true }).click()
  const center = page.getByRole('region', { name: '客服售后中心', exact: true })
  await expect(center).toBeVisible()
  await center.getByRole('combobox', { name: '办理方式', exact: true }).selectOption(type)
  await center.getByLabel('原因', { exact: true }).fill('外壳有划痕')
  await center.getByRole('button', { name: '预览申请', exact: true }).click()
  await expect(center.getByRole('region', { name: '售后确认卡' })).toContainText('1.27')
  const cases = async () => (await page.request.get(root + `/workspace/support/orders/${fixture.orderId}/cases`, { headers })).json()
  expect(await cases()).toEqual([])
  await page.screenshot({ path: info.outputPath('real-confirmation.png'), fullPage: true })
  await center.getByRole('button', { name: '确认' + label, exact: true }).click()
  await expect(center.getByText('申请已受理，请查看下方办理进度。', { exact: true })).toBeVisible()
  const rows = await cases(); expect(rows).toHaveLength(1); expect(rows[0].phase).toBe(type === 'REFUND_ONLY' ? 'AWAITING_REVIEW' : 'AWAITING_RETURN')
  async function admin(path: string, data = {}) {
    // Independent test actor; token is never passed to the browser or model.
    const response = await fetch(fixture.authority + '/api/admin/support-simulator' + path, { method: 'POST', headers: { Authorization: 'Bearer ' + fixture.adminToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'browser-' + randomUUID() }, body: JSON.stringify(data) })
    expect(response.ok).toBeTruthy(); return (await response.json()).data
  }
  async function receipt(event: string, fields = {}) {
    const current = (await cases())[0]
    const r = await admin(`/cases/${current.id}/receipts`, { event, expectedVersion: current.version, reason: '独立浏览器验收', ...fields })
    await admin(`/receipts/${r.id}/apply`)
  }
  if (type === 'REFUND_ONLY') await receipt('APPROVE')
  else {
    await center.getByLabel('寄回单号', { exact: true }).fill('RETURN-BROWSER-001')
    await center.getByRole('button', { name: '登记寄回', exact: true }).click()
    await expect(center.getByText('已登记寄回，等待仓库收货与验收。', { exact: true })).toBeVisible()
    await expect.poll(async () => (await cases())[0].phase).toBe('RETURN_IN_TRANSIT')
    await receipt('RETURN_RECEIVED', { itemId: fixture.itemId, quantity: 1 })
    expect((await cases())[0].phase).toBe('AWAITING_INSPECTION')
    writeFileSync(info.outputPath('before-inspection-cases.json'), JSON.stringify(await cases(), null, 2))
    await receipt('INSPECTION_ACCEPTED', { itemId: fixture.itemId, quantity: 1, sellable: type === 'RETURN_REFUND' })
    await admin(`/cases/${rows[0].id}/process`)
  }
  if (type === 'EXCHANGE') {
    expect((await cases())[0].phase).toBe('REPLACEMENT_READY')
    const dispatch = await admin(`/cases/${rows[0].id}/replacement-dispatch`, { trackingNo: 'REPLACEMENT-BROWSER-001' })
    await admin(`/receipts/${dispatch.id}/apply`)
    const received = await admin(`/cases/${rows[0].id}/replacement-received`, { trackingNo: 'REPLACEMENT-BROWSER-001' })
    await admin(`/receipts/${received.id}/apply`)
  } else {
    const refund = await admin(`/cases/${rows[0].id}/refund-success`)
    await admin(`/receipts/${refund.id}/apply`)
  }
  await center.getByRole('button', { name: '刷新售后进度', exact: true }).click()
  const completed = center.getByRole('heading', { name: label + ' · 办理完成', exact: true })
  await expect(completed).toBeVisible()
  expect((await cases())[0].phase).toBe('COMPLETED')
  await completed.scrollIntoViewIfNeeded()
  await page.screenshot({ path: info.outputPath('real-completed.png'), fullPage: true })
  info.annotations.push({ type: 'isolated-order', description: fixture.orderId })
})

test('5173 real model query and user-confirmed ticket reply', async ({ page }, info) => {
  test.skip(process.env.SUPPORT_BROWSER_CHAT !== '1', 'Requires completed isolated refund fixture')
  test.setTimeout(150000); page.setDefaultTimeout(15000)
  const fixture = JSON.parse(readFileSync(process.env.SUPPORT_BROWSER_FIXTURE!, 'utf8'))
  const origin = process.env.PW_BASE_URL!, root = '/api/commerce-demo'
  expect(origin).toBe('http://127.0.0.1:5173')
  const login = await page.request.post(root + '/login', { headers: { Origin: origin }, data: fixture.account })
  expect(login.ok()).toBeTruthy()
  const headers = { Origin: origin, 'X-CSRF-Token': (await login.json()).csrfToken }
  page.on('request', request => { const csrf = request.headers()['x-csrf-token']; if (request.url().startsWith(origin + root) && csrf) headers['X-CSRF-Token'] = csrf })
  await page.request.get(root + '/workspace', { headers }); await page.goto('/#orders')
  await page.getByRole('button', { name: '订单详情', exact: true }).click()
  const center = page.getByRole('region', { name: '客服售后中心', exact: true })
  await center.getByLabel('向订单客服提问').fill('请查这笔已办理售后的结果及实际退款金额。')
  await center.getByRole('button', { name: '发送客服消息', exact: true }).click()
  await expect(center.locator('.support-answer').last()).toContainText('已退款 1.27 CNY', { timeout: 65000 })
  await center.getByRole('combobox', { name: '问题类型', exact: true }).selectOption('COMPLAINT')
  await center.getByLabel('问题说明', { exact: true }).fill('服务体验问题，请记录并核实。')
  await center.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(center.getByText(/工单已登记：/)).toBeVisible()
  const tickets = await (await page.request.get(root + '/workspace/support/tickets', { headers })).json()
  expect(tickets).toHaveLength(1); const ticketId = tickets[0].id
  const events = async () => (await page.request.get(root + `/workspace/support/tickets/${ticketId}/events`, { headers })).json()
  await center.getByLabel('向订单客服提问').fill('给本单已有工单补充回复：包装破损照片已保存。')
  await center.getByRole('button', { name: '发送客服消息', exact: true }).click()
  const confirm = center.getByRole('button', { name: '确认回复工单', exact: true })
  await expect(confirm).toBeVisible({ timeout: 65000 })
  expect((await events()).filter((e: { action: string }) => e.action === 'REPLY')).toHaveLength(0)
  await confirm.scrollIntoViewIfNeeded(); await page.screenshot({ path: info.outputPath('reply-before-confirm.png') })
  await confirm.click()
  await expect(center.getByText('补充说明已提交，请刷新工单查看。', { exact: true })).toBeVisible()
  const replies = (await events()).filter((e: { action: string }) => e.action === 'REPLY')
  expect(replies).toHaveLength(1); expect(replies[0].message).toContain('包装破损照片已保存')
  writeFileSync(info.outputPath('ticket-events.json'), JSON.stringify(await events(), null, 2))
  await page.screenshot({ path: info.outputPath('reply-confirmed.png') })
})
