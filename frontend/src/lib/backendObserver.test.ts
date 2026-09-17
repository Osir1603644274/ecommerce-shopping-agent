import { afterEach, expect, test, vi } from 'vitest'
import { configureObserver, observerSnapshot, startObservation, subscribeObserver } from './backendObserver'
afterEach(() => configureObserver(false))
test('disabled observer is inert', () => { expect(startObservation('/orders/page', 'GET')).toBeNull() })
test('records bounded redacted metadata only', () => {
  configureObserver(true)
  for (let i = 0; i < 35; i++) startObservation('/orders/123456?cursor=private', 'GET')?.(200, 'opaque-ticket')
  expect(observerSnapshot()).toHaveLength(30)
  expect(observerSnapshot()[0].path).toBe('/orders/:id')
  expect(JSON.stringify(observerSnapshot())).not.toContain('private')
})
test('identity change drops in-flight records and prevents recursive trace reads', () => {
  configureObserver(true)
  const finish = startObservation('/orders/page', 'GET')
  configureObserver(true)
  finish?.(200, 'old-owner')
  expect(observerSnapshot()).toHaveLength(0)
  expect(startObservation('/backend-traces/test', 'GET')).toBeNull()
})
test('subscriber is removable', () => {
  const fn = vi.fn(), stop = subscribeObserver(fn)
  configureObserver(true); expect(fn).toHaveBeenCalledTimes(1)
  stop(); configureObserver(false); expect(fn).toHaveBeenCalledTimes(1)
})
test('timestamps and explicit triggers are preserved without query credentials', () => {
  configureObserver(true)
  startObservation('/orders/page?cursor=private','GET',{label:'加载下一页订单',trigger:'user'})?.(200,'ticket')
  const event=observerSnapshot()[0]
  expect(event.cause?.label).toBe('加载下一页订单')
  expect(Date.parse(event.finishedAt)).toBeGreaterThanOrEqual(Date.parse(event.startedAt))
  expect(JSON.stringify(event)).not.toContain('private')
})
