import { test, expect } from '@playwright/test'
import { randomBytes } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import type { Workspace } from '../src/lib/workspace'

test('live phone search, platform switch, new chat, archived chat and fresh shopping preview', async ({ page, context }) => {
  test.skip(process.env.RUN_CHAT_RECOVERY_LIVE !== '1', 'Explicit local live acceptance only')
  test.setTimeout(900000)
  expect(process.env.PW_BASE_URL).toBe('http://127.0.0.1:5173')
  const out = 'D:/agent-experiments/chat-recovery-20260916/live'
  mkdirSync(out, { recursive: true })
  const base = process.env.PW_BASE_URL!
  const prefix = '/api/commerce-demo/workspace'
  const account = { username: 'recovery-test-' + randomBytes(5).toString('hex'), password: randomBytes(24).toString('hex') }
  await context.setExtraHTTPHeaders({ Origin: base, 'X-Conversation-Source': 'automated_test' })
  const auth = await page.request.post('/api/commerce-demo/register', { data: account })
  expect(auth.status()).toBe(200)
  let csrf = (await auth.json()).csrfToken
  writeFileSync(out + '/session.private.json', JSON.stringify({ account, cookies: await context.cookies(), csrf }))
  const errors: string[] = [], writes: string[] = [], turns: unknown[] = []
  const responses: unknown[] = []
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (path.startsWith(prefix)) {
      responses.push({ path, status: response.status(), timing: response.request().timing() })
      writeFileSync(out + '/http-responses.json', JSON.stringify(responses, null, 2))
    }
  })
  page.on('pageerror', e => errors.push(e.message))
  page.on('request', r => {
    // /me rotates CSRF on page boot/reload; observe the token actually used by
    // the UI, rather than issuing another /me that invalidates the UI's token.
    if (r.url().startsWith(base + '/api/') && r.headers()['x-csrf-token']) csrf = r.headers()['x-csrf-token']
    if (r.method() === 'POST') writes.push(new URL(r.url()).pathname)
  })
  async function snapshot(): Promise<Workspace> {
    const response = await page.request.get(prefix + '/control', { headers: { 'X-CSRF-Token': csrf } })
    expect(response.status(), await response.text()).toBe(200)
    return response.json()
  }
  async function ask(text: string, name: string) {
    const previous = (await snapshot()).messages.length
    await page.getByLabel('告诉我你想找什么').fill(text)
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    let state = await snapshot()
    await expect.poll(async () => {
      state = await snapshot()
      if (state.messages.length <= previous) return 'not-submitted'
      return state.run?.status
    }, { timeout: 240000, intervals: [1000, 2000, 3000] }).not.toMatch(/^(not-submitted|running|pausing)$/)
    writeFileSync(out + '/' + name + '.json', JSON.stringify(state, null, 2))
    expect(state.run?.status, state.run?.notice).toBe('completed')
    expect(state.cards.length).toBeGreaterThan(0)
    turns.push({ query: text, runId: state.run?.id, status: state.run?.status, cards: state.cards.map(c => c.id) })
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeVisible()
    return state
  }
  await page.goto('/')
  await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled({ timeout: 30000 })
  await expect(page.getByRole('button', { name: '新对话', exact: true })).toBeEnabled({ timeout: 30000 })
  const apple = await ask('苹果手机有吗？', 'apple')
  const android = await ask('安卓手机有吗？', 'android')
  expect(android.conversationId).toBe(apple.conversationId)
  await page.getByRole('button', { name: '新对话', exact: true }).click()
  await expect.poll(async () => (await snapshot()).conversationId).not.toBe(apple.conversationId)
  await expect(page.locator('.chat-message')).toHaveCount(0)
  await page.getByRole('navigation', { name: '历史会话列表' }).getByRole('button', { name: '苹果手机有吗？', exact: true }).click()
  await expect.poll(async () => (await snapshot()).conversationId).toBe(apple.conversationId)
  await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled()
  await expect(page.getByRole('table', { name: '本轮商品推荐' })).toHaveCount(2)
  await page.reload()
  await expect(page.getByRole('table', { name: '本轮商品推荐' })).toHaveCount(2)
  await page.getByRole('table', { name: '本轮商品推荐' }).last().getByRole('button', { name: '查看商品', exact: true }).first().click()
  await expect(page.getByRole('dialog', { name: '商品与交易' })).toBeVisible()
  await page.getByRole('button', { name: '预览订单', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认下单', exact: true })).toBeEnabled({ timeout: 30000 })
  const preview = await snapshot()
  writeFileSync(out + '/history-preview.json', JSON.stringify(preview, null, 2))
  await page.screenshot({ path: out + '/history-shopping.png', fullPage: true })
  await page.getByRole('button', { name: '关闭商品与交易' }).click()
  await ask('预算改成3000元以内', 'history-followup')
  await page.screenshot({ path: out + '/history-chatting.png', fullPage: true })
  // Also expose the same continuation through the all-history dialog.
  await page.getByRole('button', { name: '浏览全部记录', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '历史对话' })
  await dialog.getByRole('button', { name: /苹果手机有吗/ }).click()
  await dialog.getByRole('button', { name: '继续这段对话', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByLabel('告诉我你想找什么')).toBeEnabled()
  expect(errors).toEqual([])
  expect(writes.some(p => /\/(confirm|purchase|payment-confirm)$/.test(p))).toBe(false)
  writeFileSync(out + '/ACCEPTANCE.json', JSON.stringify({ status: 'PASS', turns, writes, errors, noOrderSubmitted: true }, null, 2))
})
