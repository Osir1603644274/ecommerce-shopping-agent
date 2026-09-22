import { test, expect } from '@playwright/test'
import { demoOrders } from '../src/lib/demo'

test('support preview requires explicit confirmation, retries reuse key, tickets remain separate', async ({ page }, testInfo) => {
  const order = { ...demoOrders[2], id: 'support-order-1', items: demoOrders[2].items.map(i => ({ ...i, orderId: 'support-order-1' })) }
  const row = { id: 'case-1', orderId: order.id, itemId: order.items[0].itemId, quantity: 1, type: 'RETURN_REFUND', phase: 'AWAITING_RETURN', amountMinor: order.payableMinor, currency: 'CNY', specification: '{"label":"原规格"}', version: 0 }
  let confirmed = false, attempts = 0, cancellations = 0, replies = 0, ticket: Record<string, unknown> | null = null
  const conversation: { enabled: boolean; turns: object[] } = { enabled: true, turns: [] }
  const keys: string[] = []
  await page.route('**/api/commerce-demo/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/commerce-demo', '')
    const send = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/me') return send({ authenticated: true, username: 'alice', csrfToken: 'alice-csrf' })
    if (path === '/capability') return send({ enabled: true })
    if (path === '/workspace') return send({ messages: [], cards: [], selection: null, checkout: null, csrfToken: 'alice-csrf' })
    if (path === '/orders/page') return send({ orders: [order], nextCursor: null, hasMore: false })
    if (path === `/orders/${order.id}`) return send(order)
    if (path.endsWith(`/orders/${order.id}/conversation`)) {
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        if (body.message.includes('补充回复')) {
          conversation.turns.push({ ...body, status: 'COMPLETED', result: { answer: '核对后确认回复，尚未写入工单。', citations: [], preview: null, ticketDraft: null,
            ticketReplyDraft: { orderId: order.id, ticketId: 'ticket-1', body: { expectedVersion: 0, message: '包装右侧破损' } } } })
          return send(conversation)
        }
        const cancel = body.message.includes('撤销')
        conversation.turns.push({ ...body, status: 'COMPLETED', result: { answer: cancel ? '请核对撤销操作，尚未提交。' : '已出库，尚未签收。', citations: [{ id: 'fact-1', kind: 'logistics', observedAt: '2026-09-19T00:00:00Z' }], preview: null, ticketDraft: null,
          actionDraft: cancel ? { orderId: order.id, caseId: 'case-1', action: 'cancel', label: '撤销售后申请', body: { expectedVersion: 0 } } : null } })
      }
      return send(conversation)
    }
    if (path.endsWith('/after-sales')) return send({ order, balance: [{ itemId: row.itemId, quantity: 1, refundedQuantity: 0, refundedMinor: 0 }], fulfillment: { status: 'RECEIVED', trackingNo: 'SIM-1' } })
    if (path.endsWith(`/orders/${order.id}/cases`)) return send(confirmed ? [row] : [])
    if (path === '/workspace/support/preview') {
      expect(request.postDataJSON().type).toBe('RETURN_REFUND')
      return send({ previewId: 'preview-1', amountMinor: row.amountMinor, currency: 'CNY', expiresAt: '2099-01-01T00:00:00Z', specification: row.specification })
    }
    if (path === '/workspace/support/confirm') {
      attempts++; keys.push(request.headers()['idempotency-key']); confirmed = true
      if (attempts === 1) return route.abort('failed') // server committed, browser response lost
      return send(row)
    }
    if (path === '/workspace/support/cases/case-1/cancel') {
      cancellations++; expect(request.postDataJSON()).toEqual({ expectedVersion: 0 })
      expect(request.headers()['idempotency-key']).toBeTruthy()
      row.phase = 'CANCELLED'; return send(row)
    }
    if (path === '/workspace/support/tickets') {
      if (request.method() === 'POST') {
        ticket = { ...request.postDataJSON(), id: 'ticket-1', status: 'OPEN', version: 0 }
        return send(ticket)
      }
      return send(ticket ? [ticket] : [])
    }
    if (path.endsWith('/tickets/ticket-1/events')) return send([{ id: 'event-1', actor: 'alice', action: 'CREATED', message: '请核实寄回情况', createdAt: '2026-09-19T00:00:00Z' }])
    if (path.endsWith('/tickets/ticket-1/reply')) {
      replies++; expect(request.postDataJSON()).toEqual({ expectedVersion: 0, message: '包装右侧破损' })
      expect(request.headers()['idempotency-key']).toBeTruthy(); return send({ ...ticket, version: 1 })
    }
    return send({}, 404)
  })
  await page.goto('/#orders')
  await page.getByRole('button', { name: '订单详情', exact: true }).click()
  const center = page.getByRole('region', { name: '客服售后中心', exact: true })
  await center.getByLabel('向订单客服提问').fill('原订单商品到哪了')
  await center.getByRole('button', { name: '发送客服消息' }).click()
  await expect(center.getByText('已出库，尚未签收。', { exact: false })).toBeVisible()
  expect(attempts).toBe(0)
  await page.reload()
  await page.getByRole('button', { name: '订单详情', exact: true }).click()
  await expect(center.getByText('已出库，尚未签收。', { exact: false })).toBeVisible()
  await center.getByText('查看回答依据', { exact: true }).click()
  await center.getByRole('region', { name: '订单客服对话', exact: true }).screenshot({ path: testInfo.outputPath('support-chat-evidence.png') })
  await center.getByLabel('原因', { exact: true }).fill('商品损坏')
  await center.getByRole('button', { name: '预览申请', exact: true }).click()
  await expect(center.getByRole('region', { name: '售后确认卡' })).toBeVisible()
  expect(attempts).toBe(0)
  await center.getByRole('button', { name: '确认退货退款', exact: true }).click()
  await expect(center.getByRole('alert')).toContainText('无法连接服务')
  await center.getByRole('button', { name: '刷新售后进度' }).click()
  await expect(center.getByRole('heading', { name: '退货退款 · 请登记寄回单号' })).toBeVisible()
  await center.getByRole('button', { name: '确认退货退款', exact: true }).click()
  await expect(center.getByRole('region', { name: '售后确认卡' })).toHaveCount(0)
  expect(attempts).toBe(2); expect(keys[0]).toBeTruthy(); expect(keys[1]).toBe(keys[0])
  await center.getByLabel('问题类型').selectOption('AFTERSALE_DISPUTE')
  await center.getByLabel('关联售后').selectOption('case-1')
  await center.getByLabel('问题说明').fill('请核实寄回情况')
  await center.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(center.getByText('工单已登记：ticket-1', { exact: true })).toBeVisible()
  await expect(center.getByRole('heading', { name: '退货退款 · 请登记寄回单号' })).toBeVisible()
  await center.getByLabel('向订单客服提问').fill('撤销这笔售后')
  await center.getByRole('button', { name: '发送客服消息' }).click()
  await expect(center.getByRole('button', { name: '确认撤销售后申请', exact: true })).toBeVisible()
  expect(cancellations).toBe(0)
  await center.getByRole('button', { name: '确认撤销售后申请', exact: true }).click()
  await expect(center.getByText('操作已提交，请刷新售后进度核对最新状态。', { exact: true })).toBeVisible()
  expect(cancellations).toBe(1)
  await center.getByLabel('向订单客服提问').fill('补充回复：包装右侧破损')
  await center.getByRole('button', { name: '发送客服消息' }).click()
  await expect(center.getByRole('button', { name: '确认回复工单', exact: true })).toBeVisible()
  expect(replies).toBe(0)
  await center.getByRole('button', { name: '确认回复工单', exact: true }).click()
  await expect(center.getByText('补充说明已提交，请刷新工单查看。', { exact: true })).toBeVisible()
  expect(replies).toBe(1)
  await center.getByRole('button', { name: '刷新售后进度' }).click()
  await expect(center.getByRole('heading', { name: '退货退款 · 申请已撤销' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('support-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('support-mobile.png'), fullPage: true })
})
