import { describe, it, expect } from 'vitest'
import { requestJourney } from './requestJourney'
describe('one user action and its Java calls', () => {
  const calls = ['/api/products/:id','/api/products/:id/offer','/api/identity/me','/api/orders/preview'].map(path => ({path,method:'GET',status:200}))
  it('explains repeated preview reads without claiming they were removed', () => {
    const result=requestJourney(calls,'/workspace/preview')
    expect(result.notes).toHaveLength(4)
    expect(result.notes[0].title).toBe('刷新选中商品的资料')
    expect(result.notes[1].why).toContain('记录中的两次读取')
    expect(result.notes[2].title).toBe('核实本次交易的登录身份')
    expect(result.notes[3].why).toContain('不会建单、占库存或扣款')
    expect(result.explanation).toContain('不是多次下单')
  })
  it('does not reuse preview claims for selection or payment preview', () => {
    expect(requestJourney(calls,'/workspace/selection').notes[0].title).toBe('读取商品详情')
    expect(requestJourney(calls,'/workspace/payment-preview').explanation).not.toContain('预览订单')
  })
  it('does not invent omitted requests', () => {
    expect(requestJourney(calls.slice(0,1),'/workspace/preview').notes).toHaveLength(1)
  })
})
