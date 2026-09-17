import { test, expect } from '@playwright/test'
import type { Route } from '@playwright/test'

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
const state = { conversationId: '0123456789abcdef0123456789abcdef', messages: [], cards: [], selection: null, checkout: null, csrfToken: 'guest-csrf' }

test('expired workspace opens login and restores usable controls after login', async ({ page }) => {
  let loggedIn = false
  await page.route('**/api/commerce-demo/**', route => {
    const path = new URL(route.request().url()).pathname.split('/commerce-demo')[1]
    if (path === '/capability') return json(route, { enabled: true })
    if (path === '/me') return json(route, { detail: 'authentication required' }, 401)
    if (path === '/login') { loggedIn = true; return json(route, { authenticated: true, username: 'alice', csrfToken: 'new-csrf' }) }
    if (path === '/workspace/favorites') return json(route, { products: [] })
    return loggedIn ? json(route, state) : json(route, { detail: 'authentication expired' }, 401)
  })
  await page.goto('/')
  const dialog = page.getByRole('dialog', { name: '登录账户' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('用户名', { exact: true }).fill('alice')
  await dialog.getByLabel('密码', { exact: true }).fill('test-password')
  await dialog.locator('button[type=submit]').click()
  await expect(dialog).toHaveCount(0)
  await page.getByLabel('告诉我你想找什么').fill('无糖可乐 888ml')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '浏览全部记录', exact: true })).toBeEnabled()
})

test('backend outage exposes retry beside composer and preserves draft', async ({ page }) => {
  let available = false
  await page.route('**/api/commerce-demo/**', route => {
    const path = new URL(route.request().url()).pathname.split('/commerce-demo')[1]
    if (path === '/capability') return json(route, { enabled: true })
    if (path === '/me') return json(route, { detail: 'authentication required' }, 401)
    return available ? json(route, state) : json(route, { detail: 'unavailable' }, 502)
  })
  await page.goto('/')
  const composer = page.locator('.chat-composer')
  await expect(composer.getByRole('status')).toContainText('服务暂时不可用')
  await page.getByLabel('告诉我你想找什么').fill('保留我的需求')
  available = true
  await composer.getByRole('button', { name: '重新连接', exact: true }).click()
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await expect(page.getByLabel('告诉我你想找什么')).toHaveValue('保留我的需求')
})

test('expiry on send keeps draft and offers login', async ({ page }) => {
  await page.route('**/api/commerce-demo/**', route => {
    const path = new URL(route.request().url()).pathname.split('/commerce-demo')[1]
    if (path === '/me') return json(route, { detail: 'authentication required' }, 401)
    if (path === '/capability') return json(route, { enabled: true })
    if (path === '/workspace/run') return json(route, { detail: 'authentication expired' }, 401)
    return json(route, state)
  })
  await page.goto('/')
  await page.getByLabel('告诉我你想找什么').fill('无糖可乐 888ml')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByRole('dialog', { name: '登录账户' })).toBeVisible()
  await expect(page.getByLabel('告诉我你想找什么')).toHaveValue('无糖可乐 888ml')
})
