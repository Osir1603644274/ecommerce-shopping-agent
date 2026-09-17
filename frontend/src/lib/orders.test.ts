import { describe, expect, it } from 'vitest'
import { demoOrders, demoPage } from './demo'
import {
  dateLabel,
  mergeOrders,
  money,
  pageQuery,
  parseOrder,
  parsePage,
  parseSession,
} from './orders'
import { retrySeconds } from './api'

describe('order contract and display', () => {
  it('accepts Java order DTO and discards opaque evidence', () => {
    const parsed = parseOrder({ ...demoOrders[0], extra: 'ignored' })
    expect(parsed.items).toHaveLength(2)
    expect(parsed).not.toHaveProperty('extra')
  })
  it('rejects wrong owner/item shape, unsafe money and unknown status', () => {
    expect(() => parseOrder({ ...demoOrders[0], status: 'DELIVERED' })).toThrow()
    expect(() => parseOrder({ ...demoOrders[0], payableMinor: 12.5 })).toThrow()
    expect(() =>
      parseOrder({ ...demoOrders[0], totalMinor: Number.MAX_SAFE_INTEGER + 1 }),
    ).toThrow()
    expect(() =>
      parseOrder({
        ...demoOrders[0],
        items: [{ ...demoOrders[0].items[0], orderId: 'someone-else' }],
      }),
    ).toThrow()
  })
  it('validates cursor/result consistency', () => {
    expect(parsePage({ orders: [], nextCursor: null, hasMore: false }).orders).toEqual([])
    expect(() => parsePage({ orders: [], nextCursor: null, hasMore: true })).toThrow()
    expect(() => parsePage({ orders: [], nextCursor: 'old', hasMore: false })).toThrow()
    expect(() => parsePage('<html>bad gateway</html>')).toThrow()
  })
  it('preserves opaque cursor and resets query for first page', () => {
    const cursor = 'v1+a/b=.签名'
    expect(new URLSearchParams(pageQuery('PAID', cursor)).get('cursor')).toBe(cursor)
    expect(pageQuery('')).toBe('size=20')
    expect(new URLSearchParams(pageQuery('REFUNDED')).has('cursor')).toBe(false)
  })
  it('deduplicates overlap while updating existing rows', () => {
    const first = demoOrders[0]
    const result = mergeOrders([first], [{ ...first, status: 'PAID' }, demoOrders[1]])
    expect(result).toHaveLength(2)
    expect(result[0].status).toBe('PAID')
  })
  it('formats integer minor units and avoids inventing timezone', () => {
    expect(money(101)).toContain('1.01')
    expect(dateLabel('2026-09-08T10:30:22.123')).toBe('2026-09-08 10:30')
    expect(dateLabel(null)).toBe('—')
  })
  it('only accepts authenticated session and does not retain JWT fields', () => {
    expect(
      parseSession({
        authenticated: true,
        username: 'user',
        csrfToken: 'csrf',
        accessToken: 'secret',
      }),
    ).not.toHaveProperty('accessToken')
    expect(() =>
      parseSession({ authenticated: false, username: 'user', csrfToken: 'csrf' }),
    ).toThrow()
  })
  it('sample pagination is explicit, deterministic and status-filtered', () => {
    const page = demoPage('')
    expect(page.orders).toHaveLength(20)
    expect(demoPage('', page.nextCursor).orders).toHaveLength(6)
    expect(demoPage('PAID').orders.every((order) => order.status === 'PAID')).toBe(true)
  })
  it('reads numeric and date Retry-After without assuming a Lua result', () => {
    expect(retrySeconds('30')).toBe(30)
    expect(retrySeconds('bad')).toBeNull()
    expect(retrySeconds(new Date(Date.now() + 10_000).toUTCString())).toBeGreaterThanOrEqual(9)
  })
})
