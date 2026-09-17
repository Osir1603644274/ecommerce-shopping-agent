import { describe, expect, it } from 'vitest'
import { describeEvent, explainObserved, flowDefinition, learningFlow, type LearningEvent } from './learningFlow'
const event = (name: string, extra: Partial<LearningEvent> = {}): LearningEvent => ({ name, kind:'method', outcome:'returned', affectedRows:null, ...extra })
describe('learning explanations stay evidence bounded', () => {
  it('explains source identity resolution without granting a purchase', () => {
    const step=describeEvent(event('ProductSourceResolutionController.resolve'))
    expect(step.title).toContain('来源')
    expect(step.description).toContain('原文校验值')
    expect(step.description).toContain('不创建订单')
  })
  it('explains each real product and preview method distinctly', () => {
    const names=['ProductController.get','ProductService.get','IdentityController.me','OrderController.preview','OrderService.preview','CommerceCatalogReadService.requireItem','CouponService.quote']
    const steps=learningFlow(names.map(name=>event(name))).steps
    expect(steps).toHaveLength(names.length)
    expect(new Set(steps.map(s=>s.title)).size).toBe(names.length)
    expect(steps.some(s=>s.title.includes('执行业务处理') || s.title.includes('待补充'))).toBe(false)
    expect(steps.find(s=>s.title.includes('试算金额'))?.why).toContain('不占库存')
  })
  it('never merges different unmapped methods by a fallback title', () => {
    expect(learningFlow([event('OtherService.a'),event('OtherService.b')]).steps).toHaveLength(2)
  })
  it('distinguishes recorded local and Redis caches without inventing an absent tier', () => {
    const local=event('商品详情 l1',{kind:'cache',outcome:'hit'})
    const remote=event('商品详情 l2',{kind:'cache',outcome:'miss'})
    expect(describeEvent(local).title).toContain('Java 内存')
    expect(describeEvent(remote).title).toContain('Redis')
    expect(learningFlow([local]).steps).toHaveLength(1)
    expect(explainObserved(remote).join('')).toContain('不能把缓存未命中解释成商品不存在')
  })
  it('interprets product wrappers without irrelevant payment language', () => {
    const text=explainObserved(event('ProductController.get',{input:{id:'2277072270369927985'},output:{type:'ResponseEntity'}})).join('')
    expect(text).toContain('2277072270369927985')
    expect(text).not.toContain('支付')
    expect(text).not.toContain('订单')
  })
  it('reads single-item preview input and does not equate preview to reservation', () => {
    const text=explainObserved(event('OrderService.preview',{input:{request:{itemId:'123',quantity:2}}})).join('')
    expect(text).toContain('商品 123，数量 2 件')
    expect(text).toContain('不会创建订单或预占库存')
  })
  it('explains the actual cart amounts instead of inventing a payment receipt', () => {
    const e = event('CartOrderController.create', { input:{request:{items:[{itemId:'1710698',quantity:1,expectedUnitPriceMinor:69300}],expectedPayableMinor:69300}}, output:{type:'ResponseEntity'} })
    expect(explainObserved(e).join(' ')).toContain('693.00 元')
    expect(explainObserved(e).join(' ')).toContain('不是支付回执')
    expect(explainObserved(e).join(' ')).toContain('没有记录完整订单内容')
    expect(describeEvent(e).title).not.toContain('执行业务')
  })
  it('does not invent a write stage on idempotent replay', () => {
    const result=learningFlow([event('CartOrderService.create'),event('OrderMapper.findByIdempotencyKey [SELECT]'),event('OrderService.get')])
    expect(result.steps.map(s=>s.title)).not.toContain('保存订单并预占库存')
    expect(result.steps.map(s=>s.title)).toContain('读取订单作为返回内容')
  })
  it('collapses stock work but retains every event and shows rollback last', () => {
    const events=[event('CartOrderService.create',{id:'cart'}),event('数据库事务',{kind:'transaction',outcome:'rolled_back'}),event('InventoryService.reserve',{id:'reserve',parentId:'cart'}),event('InventoryMapper.findStock [SELECT]',{parentId:'reserve',kind:'sql'})]
    const flow=learningFlow(events)
    expect(flow.steps.find(s=>s.title==='保存订单并预占库存')?.events).toHaveLength(2)
    expect(flow.steps.at(-1)?.state).toBe('failed')
    expect(flow.steps.flatMap(s=>s.events)).toHaveLength(events.length)
    expect(explainObserved(events[1]).join('')).toContain('不会作为成功结果保留')
  })
  it('does not mistake zero discount for no coupon or missing output for success', () => {
    expect(explainObserved(event('CouponService.consume',{output:0})).join('')).toContain('不能断言')
    expect(explainObserved(event('InventoryMapper.reserve',{kind:'sql',outcome:'executed',affectedRows:0})).join('')).toContain('不能认定预占成功')
    expect(explainObserved(event('数据库事务',{kind:'transaction',outcome:'unknown'})).join('')).toContain('不能把此前写入当作最终成功')
  })
  it('does not copy untrusted names or input into Mermaid', () => {
    const flow=learningFlow([event('CartOrderController.create',{input:{request:{items:[{itemId:'<script>alert(1)</script>',quantity:1}]}}}),event('x["evil"]\nclick x "https://evil.test"')])
    const source=flowDefinition(flow.steps)
    expect(source).not.toContain('evil')
    expect(source).not.toContain('script')
  })
  it('handles cyclic or missing parent data without loops', () => {
    const events=[event('CartOrderController.create'),event('unknown',{id:'x',parentId:'y'}),event('unknown',{id:'y',parentId:'x'})]
    expect(learningFlow(events).steps.flatMap(s=>s.events)).toHaveLength(3)
  })
})
