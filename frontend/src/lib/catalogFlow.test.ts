import { describe, expect, it } from 'vitest'
import { catalogFlowDefinition } from './catalogFlow'

describe('catalog architecture is grounded in checkpoints', () => {
  it('shows a fixed search path and pending stages', () => {
    const text = catalogFlowDefinition([{label:'整理需求',outcome:'completed',detail:{phase:'prepare',action:'search'}}])
    expect(text).toContain('需要搜索')
    expect(text).toContain('class P done')
    expect(text).toContain('class A pending')
    expect(text).not.toContain('S --> P')
  })
  it('shows reuse rather than fake retrieval for comparison', () => {
    const text=catalogFlowDefinition([{label:'整理需求',outcome:'completed',detail:{phase:'prepare',action:'compare'}}])
    expect(text).toContain('读取已保存候选')
    expect(text).not.toContain('调用两个商品来源检索')
  })
  it('never inserts user data into Mermaid', () => {
    const text=catalogFlowDefinition([{label:'<script>danger</script>',input:{query:'evil\nclick U evil'},detail:{phase:'prepare',action:'search'}}])
    expect(text).not.toContain('evil')
    expect(text).not.toContain('script')
  })
})
