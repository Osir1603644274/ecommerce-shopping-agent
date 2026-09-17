import { expect, it } from 'vitest'
import { eventExplanation, outcomeExplanation, requestExplanation } from './explainExecution'
it('explains known business methods without leaking package names into titles', () => {
  expect(eventExplanation({kind:'sql',name:'com.example.locallife.product.ProductMapper.findById [SELECT]',outcome:'executed'}).title).toBe('读取商品资料')
  expect(requestExplanation('/api/products/:id/offer')).toBe('核对商品价格与库存')
  expect(outcomeExplanation('threw:IllegalStateException')).toBe('处理时发生异常')
})
it('does not invent domain semantics or success for unknown methods', () => {
  expect(eventExplanation({kind:'method',name:'unknown.Service.foo',outcome:'unrecognized'}).title).toBe('执行业务处理')
  expect(outcomeExplanation('unrecognized')).toBe('查看处理结果')
})
