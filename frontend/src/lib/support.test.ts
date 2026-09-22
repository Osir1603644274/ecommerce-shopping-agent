import { describe, expect, it } from 'vitest'
import { parseSupportCase, parseSupportPreview, parseSupportTicket, parseTicketEvent, supportRequestKey } from './support'

const row = { id: 'case-1', orderId: 'order-1', itemId: '9007199254740993', quantity: 1, type: 'EXCHANGE', phase: 'WAITING_STOCK', amountMinor: 999, currency: 'CNY', specification: null, version: 2 }
describe('support browser authority boundary', () => {
  it('keeps Java BIGINT IDs exact and rejects unsafe amounts or unknown outcomes', () => {
    expect(parseSupportCase(row).itemId).toBe('9007199254740993')
    for (const patch of [{ itemId: 9007199254740992 }, { amountMinor: 9.5 }, { phase: 'REFUND_SUCCEEDED' }, { specification: {} }])
      expect(() => parseSupportCase({ ...row, ...patch })).toThrow()
  })
  it('requires a valid confirmation expiry and integer amount', () => {
    const preview = { previewId: 'preview-1', amountMinor: 999, currency: 'CNY', expiresAt: '2026-09-19T00:00:00Z' }
    expect(parseSupportPreview(preview)).toEqual(preview)
    expect(() => parseSupportPreview({ ...preview, expiresAt: 'tomorrow' })).toThrow()
    expect(() => parseSupportPreview({ ...preview, amountMinor: Number.MAX_SAFE_INTEGER + 1 })).toThrow()
  })
  it('reuses an operation key after a lost response but separates versions, paths and orders', async () => {
    const key = await supportRequestKey('order-1', '/cancel', { expectedVersion: 2 })
    expect(await supportRequestKey('order-1', '/cancel', { expectedVersion: 2 })).toBe(key)
    expect(await supportRequestKey('order-1', '/cancel', { expectedVersion: 3 })).not.toBe(key)
    expect(await supportRequestKey('order-2', '/cancel', { expectedVersion: 2 })).not.toBe(key)
    expect(await supportRequestKey('order-1', '/wait-stock', { expectedVersion: 2 })).not.toBe(key)
  })
  it('rejects incomplete ticket state and communication records', () => {
    const ticket = { id: 't-1', orderId: 'o-1', caseId: null, category: 'DELIVERY_DELAY', status: 'OPEN', summary: '物流催办', version: 0 }
    expect(parseSupportTicket(ticket).status).toBe('OPEN')
    expect(() => parseSupportTicket({ ...ticket, status: 'REFUNDED' })).toThrow()
    expect(() => parseTicketEvent({ id: 'e-1', message: '已处理' })).toThrow()
  })
})
