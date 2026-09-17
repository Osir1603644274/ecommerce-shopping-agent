import { test, expect } from '@playwright/test'
import { randomBytes } from 'node:crypto'

test('live local BFF + Java + Redis + MySQL: register, authenticated empty page, logout', async ({
  page,
}) => {
  test.skip(
    process.env.RUN_LIVE_COMMERCE !== '1',
    'Opt-in: creates one isolated QA account, never creates/pays an order.',
  )
  const username = `frontend-qa-${Date.now()}`
  const password = randomBytes(20).toString('hex')
  await page.goto('/legacy-orders')
  await page.getByRole('button', { name: '登录查看订单' }).click()
  await page.getByRole('button', { name: '创建账户' }).click()
  await page.getByLabel('用户名', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByLabel('确认密码').fill(password)
  await page.getByRole('button', { name: '注册并登录' }).click()
  await expect(page.locator('.account-name')).toContainText(username)
  await expect(page.getByRole('heading', { name: '你的第一笔好选择，还在路上' })).toBeVisible()
  const cookie = (await page.context().cookies()).find(
    (cookie) => cookie.name === 'commerce_demo_session',
  )
  expect(cookie?.httpOnly).toBe(true)
  expect(await page.evaluate(() => document.cookie)).not.toContain('commerce_demo_session')
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0])
  await page.reload()
  await expect(page.locator('.account-name')).toContainText(username)
  await expect(page.getByRole('heading', { name: '你的第一笔好选择，还在路上' })).toBeVisible()
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('button', { name: '登录查看订单' })).toBeEnabled()
  await expect
    .poll(async () =>
      (await page.context().cookies()).some((cookie) => cookie.name === 'commerce_demo_session'),
    )
    .toBe(false)
  // Confirm existing-account login as a separate real endpoint, not only registration.
  await page.getByRole('button', { name: '登录查看订单' }).click()
  await page.getByLabel('用户名', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录账户', exact: true }).click()
  await expect(page.locator('.account-name')).toContainText(username)
  await expect(page.getByRole('heading', { name: '你的第一笔好选择，还在路上' })).toBeVisible()
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('button', { name: '登录查看订单' })).toBeEnabled()
  // Never record credentials, session ids or CSRF tokens in the report.
  test
    .info()
    .annotations.push({
      type: 'scope',
      description:
        'Real local account/auth and empty order page only; no order mutation or model calls.',
    })
})
