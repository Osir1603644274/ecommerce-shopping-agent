import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { describeEvent, explainObserved, learningFlow, type LearningEvent } from './learningFlow'
import { learningCatalog } from './learningCatalog'

const root = fileURLToPath(new URL('../../../backend/src/main/java/com/example/locallife/', import.meta.url))
// Discover modules so adding a new Java module cannot silently escape coverage.
const modules = readdirSync(root, { withFileTypes: true })
  .filter(dir => dir.isDirectory() && dir.name !== 'diagnostics').map(dir => dir.name)
const event = (name: string, extra: Partial<LearningEvent> = {}): LearningEvent => ({
  kind: 'method', name, outcome: 'returned', affectedRows: null, ...extra,
})

/** Conservative source inventory, not a Java parser or runtime call-coverage claim.
 * Strip literals/comments before counting braces, ignore nested record/DTO methods,
 * and match public instance methods (observer pointcut) or Mapper declarations.
 */
function declarations(source: string, mapper: boolean) {
  const plain = source.replace(/"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, m => m.replace(/[^\n]/g, ' '))
  let depth = 0
  const names: string[] = []
  for (const line of plain.split('\n')) {
    if (depth === 1) {
      const match = /^\s*(public\s+)?([\w.<>?,[\] ]+)\s+(\w+)\s*\(/.exec(line)
      if (match && (mapper || match[1]) && !/\b(static|class|record|interface|private|protected)\b/.test(match[2])) names.push(match[3])
    }
    depth += (line.match(/\{/g)?.length ?? 0) - (line.match(/\}/g)?.length ?? 0)
  }
  return [...new Set(names)]
}

describe('reviewed Java explanation coverage', () => {
  it('discovers real instance methods without constructors or nested DTO accessors', () => {
    expect(declarations(`public class ExampleService {
      public ExampleService(String name) {}
      public String get(Long id) { return "{"; }
      public static String key(Long id) { return "}"; }
      public record View(String id) {}
      private String helper() { return ""; }
    }`, false)).toEqual(['get'])
  })
  it('covers every discoverable public observer method and Mapper declaration', () => {
    const missing: string[] = []
    let count = 0
    for (const module of modules) {
      for (const file of readdirSync(`${root}/${module}`).filter(f => /(?:Service|Controller|Cache|RedisGateway|Mapper)\.java$/.test(f))) {
        const klass = file.replace('.java', '')
        const source = readFileSync(`${root}/${module}/${file}`, 'utf8')
        for (const name of declarations(source, klass.endsWith('Mapper'))) {
          const symbol = `${klass}.${name}`
          count++
          if (describeEvent(event(symbol, { kind: klass.endsWith('Mapper') ? 'sql' : 'method' })).title.includes('待补充')) missing.push(symbol)
        }
      }
    }
    expect(count).toBeGreaterThan(300) // Prevent a broken inventory from silently testing zero methods.
    expect(missing, `Missing reviewed descriptions among ${count} Java symbols`).toEqual([])
  })
  it('never turns unknown future methods into invented explanations', () => {
    expect(describeEvent(event('FutureService.unknown')).title).toContain('待补充')
    expect(learningFlow([event('FutureService.a'), event('FutureService.b')]).steps).toHaveLength(2)
  })
  it('all added entries have meaningful, separate titles and descriptions', () => {
    for (const [symbol, [title, description]] of Object.entries(learningCatalog)) {
      expect(title, symbol).not.toMatch(/待补充|执行业务处理|访问业务数据/)
      expect(description.length, symbol).toBeGreaterThan(15)
    }
  })
  it('explains cache branches and does not call Redis fallback a real acquired lock', () => {
    expect(describeEvent(event('ProductCache.getDetail')).description).toContain('本机未命中才读取 Redis')
    expect(describeEvent(event('ProductCache.tryLockDetail')).description).toContain('不一定表示真正取得了 Redis 锁')
    expect(explainObserved(event('ProductCache.putDetail', { outcome: 'threw:RuntimeException' })).join('')).toContain('不表示其中所有步骤都执行成功')
  })
  it('does not mistake an order or refund identifier for a product', () => {
    const order = explainObserved(event('OrderService.cancel', { input: { orderId: 'order-123' } })).join('')
    const refund = explainObserved(event('PartialRefundService.get', { input: { id: 'refund-123' } })).join('')
    expect(order).toContain('订单编号是 order-123')
    expect(refund).toContain('退款编号是 refund-123')
    expect(order + refund).not.toContain('商品编号')
    expect(explainObserved(event('OtherService.get', { input: { id: 'unknown-id' } })).join('')).not.toContain('商品编号')
  })
  it('does not tell a registration caller about missing order or payment results', () => {
    const text = explainObserved(event('AuthController.register', { output: { type: 'ResponseEntity' } })).join('')
    expect(text).not.toMatch(/订单|支付成功/)
    expect(text).toContain('不能仅凭这个类型判断操作成功')
  })
  it('distinguishes local deal input from ordinary product input', () => {
    expect(explainObserved(event('OrderService.preview', { input: { request: { itemType: 'LOCAL_DEAL', itemId: '123', quantity: 1 } } })).join('')).toContain('到店消费商品 123')
  })
  it('does not describe the disabled legacy memory endpoint as a successful write', () => {
    expect(describeEvent(event('ShoppingMemoryController.write')).description).toContain('当前不会调用旧写入服务')
    expect(describeEvent(event('ShoppingMemoryV3Service.validateCandidate')).description).toContain('不写入偏好')
  })
})
