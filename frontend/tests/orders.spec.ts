import { test, expect } from '@playwright/test'
import type { Page, Route } from '@playwright/test'
import { demoOrders } from '../src/lib/demo'

const session = { authenticated: true, username: 'reader-one', csrfToken: 'test-csrf' }
const pageData = (orders = [demoOrders[0]], nextCursor: string | null = null) => ({
  orders,
  nextCursor,
  hasMore: nextCursor !== null,
})
const json = (route: Route, data: unknown, status = 200, headers = {}) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data), headers })
async function setup(page: Page, handler?: (route: Route, path: string) => Promise<boolean>) {
  await page.route('**/api/commerce-demo/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/commerce-demo', '')
    if (await handler?.(route, path)) return
    if (path === '/me' || path === '/login' || path === '/register') await json(route, session)
    else if (path === '/logout') await json(route, { loggedOut: true })
    else if (path === '/orders/page') {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      await json(route, pageData())
    } else if (path.startsWith('/orders/')) await json(route, demoOrders[0])
    else await json(route, { detail: 'not found' }, 404)
  })
}

test('anonymous users see login, not orders; sample mode is explicitly labeled', async ({
  page,
}) => {
  let orderRequests = 0
  await setup(page, async (route, path) => {
    if (path === '/me') {
      await json(route, { detail: 'authentication required' }, 401)
      return true
    }
    if (path.startsWith('/orders')) orderRequests++
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('button', { name: '登录查看订单' })).toBeEnabled()
  expect(orderRequests).toBe(0)
  await page.getByRole('button', { name: '先看看示例' }).click()
  await expect(page.getByText('你正在浏览示例订单')).toBeVisible()
  await expect(page.getByRole('article')).toHaveCount(20)
  expect(orderRequests).toBe(0)
})

test('login submits credentials once and keeps JWT out of storage', async ({ page }) => {
  let logins = 0
  await setup(page, async (route, path) => {
    if (path === '/me') {
      await json(route, {}, 401)
      return true
    }
    if (path === '/login') {
      logins++
      expect(route.request().postDataJSON()).toEqual({
        username: 'reader-one',
        password: 'test-password',
      })
      await json(route, session)
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '登录查看订单' }).click()
  await page.getByLabel('用户名', { exact: true }).fill('reader-one')
  await page.getByLabel('密码', { exact: true }).fill('test-password')
  await page.getByRole('button', { name: '登录账户', exact: true }).click()
  await expect(page.getByRole('article')).toHaveCount(1)
  expect(logins).toBe(1)
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0])
  await expect(page.locator('.account-name')).toContainText('reader-one')
})

test('registration rejects mismatched password, then submits real contract', async ({ page }) => {
  let registrations = 0
  await setup(page, async (route, path) => {
    if (path === '/me') {
      await json(route, {}, 401)
      return true
    }
    if (path === '/register') {
      registrations++
      await json(route, session)
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '登录查看订单' }).click()
  await page.getByRole('button', { name: '创建账户' }).click()
  await page.screenshot({ path: 'test-results/registration-dialog.png' })
  await page.getByLabel('用户名', { exact: true }).fill('reader-one')
  await page.getByLabel('密码', { exact: true }).fill('test-password')
  await page.getByLabel('确认密码').fill('different')
  await page.getByRole('button', { name: '注册并登录' }).click()
  await expect(page.getByRole('alert')).toContainText('两次输入的密码不一致')
  expect(registrations).toBe(0)
  await page.getByLabel('确认密码').fill('test-password')
  await page.getByRole('button', { name: '注册并登录' }).click()
  await expect(page.getByRole('article')).toHaveCount(1)
  expect(registrations).toBe(1)
})

test('opaque cursor pagination deduplicates overlap and ends correctly', async ({ page }) => {
  const cursor = 'opaque+/=.cursor'
  let more = 0
  await setup(page, async (route, path) => {
    if (path !== '/orders/page') return false
    const params = new URL(route.request().url()).searchParams
    expect(params.get('size')).toBe('20')
    if (!params.has('cursor')) await json(route, pageData(demoOrders.slice(0, 20), cursor))
    else {
      more++
      expect(params.get('cursor')).toBe(cursor)
      await json(route, pageData(demoOrders.slice(19, 26)))
    }
    return true
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(20)
  await page.getByRole('button', { name: '加载更多订单' }).click()
  await expect(page.getByRole('article')).toHaveCount(26)
  await expect(page.getByText('已显示当前筛选的全部订单')).toBeVisible()
  expect(more).toBe(1)
})

test('status changes reset cursor and stale requests cannot replace new filter', async ({
  page,
}) => {
  let release: (() => void) | undefined
  let paidStarted: (() => void) | undefined
  const started = new Promise<void>((resolve) => {
    paidStarted = resolve
  })
  await setup(page, async (route, path) => {
    if (path !== '/orders/page') return false
    const params = new URL(route.request().url()).searchParams
    expect(params.has('cursor')).toBe(false)
    if (params.get('status') === 'PAID') {
      paidStarted?.()
      await new Promise<void>((resolve) => {
        release = resolve
      })
      await json(route, pageData([demoOrders[1]]))
    } else if (params.get('status') === 'COMPLETED') await json(route, pageData([demoOrders[2]]))
    else await json(route, pageData())
    return true
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(1)
  await page.getByRole('button', { name: '已支付', exact: true }).click()
  await started
  await page.getByRole('button', { name: '已完成', exact: true }).click()
  await expect(page.getByRole('article')).toHaveAccessibleName(`订单 ${demoOrders[2].orderNo}`)
  release?.()
  await expect(page.getByRole('article')).toHaveCount(1)
  await expect(page.getByRole('article')).toHaveAccessibleName(`订单 ${demoOrders[2].orderNo}`)
})

test('429 continuation preserves rows and retries with same cursor', async ({ page }) => {
  let tries = 0
  await setup(page, async (route, path) => {
    if (path !== '/orders/page') return false
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    if (!cursor) await json(route, pageData([demoOrders[0]], 'next'))
    else if (++tries === 1) await json(route, { detail: 'limited' }, 429, { 'Retry-After': '5' })
    else {
      expect(cursor).toBe('next')
      await json(route, pageData([demoOrders[1]]))
    }
    return true
  })
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '加载更多订单' }).click()
  await expect(page.getByRole('alert')).toContainText('5 秒')
  await expect(page.getByRole('article')).toHaveCount(1)
  await page.getByRole('button', { name: '重试请求' }).click()
  await expect(page.getByRole('article')).toHaveCount(2)
  expect(tries).toBe(2)
})

test('401 expiry clears orders and requires login', async ({ page }) => {
  let expire = false
  await setup(page, async (route, path) => {
    if (path === '/orders/page' && expire) {
      await json(route, {}, 401)
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(1)
  expire = true
  await page.getByRole('button', { name: '刷新订单' }).click()
  await expect(page.getByRole('alert')).toContainText('登录已过期')
  await expect(page.getByRole('article')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '登录查看订单' })).toBeEnabled()
})

test('logout clears private data and sends CSRF; next account starts without old cursor', async ({
  page,
}) => {
  let user = 'one'
  let logout = false
  await setup(page, async (route, path) => {
    if (path === '/logout') {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      logout = true
      user = 'two'
      await json(route, { loggedOut: true })
      return true
    }
    if (path === '/login') {
      await json(route, { ...session, username: 'reader-two' })
      return true
    }
    if (path === '/orders/page' && user === 'two') {
      expect(new URL(route.request().url()).searchParams.has('cursor')).toBe(false)
      await json(route, pageData([demoOrders[2]]))
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(1)
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('article')).toHaveCount(0)
  expect(logout).toBe(true)
  await page.getByRole('button', { name: '登录查看订单' }).click()
  await page.getByLabel('用户名', { exact: true }).fill('reader-two')
  await page.getByLabel('密码', { exact: true }).fill('test-password')
  await page.getByRole('button', { name: '登录账户', exact: true }).click()
  await expect(page.getByRole('article')).toHaveAccessibleName(`订单 ${demoOrders[2].orderNo}`)
})

test('details fetch latest order and dialog supports escape/focus return', async ({ page }) => {
  await setup(page, async (route, path) => {
    if (path === `/orders/${demoOrders[0].id}`) {
      await json(route, { ...demoOrders[0], status: 'PAID' })
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '订单详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('已支付')
  await expect(page.getByRole('dialog')).toContainText('优惠抵扣')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '订单详情' })).toBeFocused()
})

test('untrusted titles render as text; loaded-only search and empty states are honest', async ({
  page,
}) => {
  const title = '<img src=x onerror=alert(1)>'
  const order = { ...demoOrders[0], items: [{ ...demoOrders[0].items[0], titleSnapshot: title }] }
  await setup(page, async (route, path) => {
    if (path === '/orders/page') {
      await json(route, pageData([order]))
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.locator('.order-items img')).toHaveCount(0)
  await page.getByRole('textbox', { name: '搜索已加载订单' }).fill('not-here')
  await expect(page.getByRole('heading', { name: '已加载订单中没有匹配结果' })).toBeVisible()
  await page.getByRole('button', { name: '清空搜索', exact: true }).last().click()
  await expect(page.getByRole('article')).toHaveCount(1)
})

test('invalid payload is an error rather than fake success or empty orders', async ({ page }) => {
  await setup(page, async (route, path) => {
    if (path === '/orders/page') {
      await json(route, { orders: [], hasMore: true, nextCursor: null })
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('alert')).toContainText('游标')
  await expect(page.getByText('你的第一笔好选择，还在路上')).toHaveCount(0)
})

test('mobile sample and login fit viewport without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/?demo=1')
  await expect(page.getByRole('article')).toHaveCount(20)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.getByRole('button', { name: '订单详情' }).first().click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.width).toBeLessThanOrEqual(390)
  await page.screenshot({ path: 'test-results/mobile-orders.png' })
})

test('desktop sample visual snapshot and pagination', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1050 })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/?demo=1')
  await expect(page.getByRole('article')).toHaveCount(20)
  await page.screenshot({ path: 'test-results/desktop-orders.png' })
  await page.getByRole('button', { name: '加载更多订单' }).click()
  await expect(page.getByRole('article')).toHaveCount(26)
  expect(errors).toEqual([])
})

test('cross-tab account change rejects old CSRF without displaying another user orders', async ({
  page,
  context,
}) => {
  let currentCsrf = 'test-csrf'
  await context.route('**/api/commerce-demo/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/me')) await json(route, { ...session, csrfToken: currentCsrf })
    else if (path.endsWith('/login')) {
      currentCsrf = 'second-account-csrf'
      await json(route, { ...session, username: 'second', csrfToken: currentCsrf })
    } else if (path.endsWith('/orders/page')) {
      if (route.request().headers()['x-csrf-token'] !== currentCsrf)
        await json(route, { detail: 'csrf validation failed' }, 403)
      else
        await json(route, pageData(currentCsrf === 'test-csrf' ? [demoOrders[0]] : [demoOrders[1]]))
    } else await json(route, {}, 404)
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(1)
  const second = await context.newPage()
  await second.goto('/?demo=1')
  await second.evaluate(async () => {
    await fetch('/api/commerce-demo/login', { method: 'POST', body: '{}' })
  })
  await page.getByRole('button', { name: '刷新订单' }).click()
  await expect(page.getByRole('alert')).toContainText('其他页面更新')
  await expect(page.getByRole('article')).toHaveCount(0)
  await expect(page.locator('.account-name')).toHaveCount(0)
})

test('mode switch is disabled while logout is pending', async ({ page }) => {
  let finish: (() => void) | undefined
  await setup(page, async (route, path) => {
    if (path === '/logout') {
      await new Promise<void>((resolve) => {
        finish = resolve
      })
      await json(route, { loggedOut: true })
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('article')).toHaveCount(1)
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('button', { name: '先看看示例' })).toBeDisabled()
  await expect(page.getByRole('article')).toHaveCount(0)
  finish?.()
  await expect(page.getByRole('button', { name: '登录查看订单' })).toBeEnabled()
})

test('loading more is single flight under repeated clicks', async ({ page }) => {
  let finish: (() => void) | undefined
  let requests = 0
  await setup(page, async (route, path) => {
    if (path !== '/orders/page') return false
    if (new URL(route.request().url()).searchParams.has('cursor')) {
      requests++
      await new Promise<void>((resolve) => {
        finish = resolve
      })
      await json(route, pageData([demoOrders[1]]))
    } else await json(route, pageData([demoOrders[0]], 'next'))
    return true
  })
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '加载更多订单' }).evaluate((button) => {
    ;(button as HTMLButtonElement).click()
    ;(button as HTMLButtonElement).click()
  })
  await expect(page.getByRole('button', { name: '加载中…', exact: true })).toBeDisabled()
  expect(requests).toBe(1)
  finish?.()
  await expect(page.getByRole('article')).toHaveCount(2)
})

test('503 is recoverable and never silently switches to example data', async ({ page }) => {
  let fail = true
  await setup(page, async (route, path) => {
    if (path === '/orders/page' && fail) {
      await json(route, {}, 503)
      return true
    }
    return false
  })
  await page.goto('/legacy-orders')
  await expect(page.getByRole('alert')).toContainText('服务暂时不可用')
  await expect(page.getByRole('article')).toHaveCount(0)
  await expect(page.getByText('你正在浏览示例订单')).toHaveCount(0)
  fail = false
  await page.getByRole('button', { name: '重试请求' }).click()
  await expect(page.getByRole('article')).toHaveCount(1)
})
