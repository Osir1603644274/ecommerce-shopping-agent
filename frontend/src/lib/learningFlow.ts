import { eventExplanation, outcomeExplanation } from './explainExecution'
import { learningCatalog } from './learningCatalog'
import type { ExecutionMetadata } from '../ExecutionDetail'

export type LearningEvent = ExecutionMetadata & { kind: string; name: string; outcome: string; affectedRows: number | null }
export type LearningStep = { id: string; title: string; why: string; events: LearningEvent[]; state: 'observed' | 'failed' | 'pending'; decision?: boolean }
export const symbolOf = (e: LearningEvent) => e.name.replace(/ \[[^\]]+\]$/, '').split('.').slice(-2).join('.')
const record = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v : null
const money = (v: unknown) => number(v) == null ? null : ((v as number) / 100).toFixed(2)
const publicId = (v: unknown) => typeof v === 'string' && /^[a-zA-Z0-9-]{1,64}$/.test(v) ? v : typeof v === 'number' ? String(v) : null

const details: Record<string, [string, string]> = {
  'ProductSourceResolutionController.resolve': ['核对来源并找到商城商品编号', '逐个核对来源名称、来源内编号与原文校验值，只返回匹配且在售的商品编号。相同标题不视为同一商品；此处不读取价格、不扣库存，也不创建订单。'],
  'ProductController.get': ['接收查看商品的请求', '从地址中取出商品编号，交给商品服务读取详情；找到时返回商品，不存在时返回“商品不存在”。这里不核定本次下单金额。'],
  'ProductService.get': ['优先复用商品详情缓存', '先读商品详情缓存；命中就直接返回。未命中时才协调缓存重建并查询商品，避免所有请求一起查询数据库。具体走了哪个分支，看本次缓存记录。'],
  'IdentityController.me': ['返回当前登录用户的身份', '从已经建立的登录身份中取用户编号和角色，返回给 Agent 交易入口。不是重新登录，也不是创建订单。'],
  'OrderController.preview': ['接收商品编号和购买数量', '读取通过参数校验的预览请求，取当前登录用户，交给订单服务试算。这里返回预览内容，不创建订单。'],
  'OrderService.preview': ['试算金额并检查库存够不够', '重新取得可交易商品的单价，用单价乘数量，计算可用优惠，再检查可用库存是否足够；通过后返回应付金额和库存。此方法不写订单、不占库存，也不使用优惠券。'],
  'CommerceCatalogReadService.requireItem': ['核验商品是否允许交易', '按商品类型读取交易资料。商品必须存在且处于在售状态；优先使用本地交易报价，否则要求已验证的快照价格。缺少有效资料会拒绝继续。'],
  'CouponService.quote': ['试算优惠，不使用优惠券', '未传优惠券时返回零优惠；传了才检查归属、可用状态、有效期和金额门槛。这里只算能减多少钱，不把券标成已使用。'],
  'OrderController.create': ['接收你确认的下单请求', '读取商品、数量、登录用户和幂等键，交给订单服务创建订单。入口返回不等于支付完成。'],
  'OrderService.create': ['校验请求并创建单商品订单', '先检查幂等键，核验商品与优惠，写订单及明细，再预占库存并登记履约和订单事件。新建分支的写入由事务保护；重复请求会检查并返回旧订单。'],
  'CartOrderController.create': ['接收你确认的商品清单', '读取商品清单、登录身份和幂等键，交给订单服务。这个入口本身不负责扣库存。'],
  'CartOrderService.create': ['组织多商品下单', '校验清单并查询重复订单；新请求才继续核价、处理优惠、写入订单、预占库存和登记待发送事件。这些写入由事务保护。'],
  'OrderMapper.findByIdempotencyKey': ['查找同一次请求的订单', '按当前用户和幂等键查询旧订单。若找到，还需要核对请求内容，不能直接再建一单。'],
  'InventoryService.getStock': ['读取商品的库存配置', '取得商品对应的库存记录；这里只是读取，还没有预占库存。'],
  'InventoryMapper.findStock': ['查询可用和已预占库存', '按商品类型与编号查库存表，读取可用数量、已预占数量等字段。'],
  'InventoryService.reserve': ['为这张订单预占库存', '先查是否已经预占；没有时才尝试条件扣减可用数量，并保存预占记录。库存不足会抛出异常。'],
  'InventoryMapper.findReservation': ['核对是否已经预占', '按订单和库存记录查已有预占，防止同一订单重复占用。'],
  'InventoryMapper.reserve': ['把可用库存转成预占库存', '仅在可用数量足够时，减少可用数量并增加预占数量；更新不到记录时，上层服务会拒绝本次预占。'],
  'InventoryMapper.insertReservation': ['保存这张订单的预占凭据', '把订单、库存记录、数量和到期时间关联起来，后续支付确认或超时释放需要这份凭据。'],
  'CouponService.consume': ['计算并使用本单优惠', '未选择优惠券时返回零优惠；选择了才校验归属、有效期和门槛，并尝试标记已使用。调用此方法不等于一定用了券。'],
  'OrderMapper.insertOrder': ['写入待支付订单', '保存订单主记录、总额、优惠和应付金额。此处写入仍受事务约束，不等于已经付款。'],
  'OrderMapper.insertItem': ['保存订单里的商品明细', '保存商品名称快照、单价、数量和小计。这样以后查询订单不必依赖商品当前的名称或价格。'],
  'OutboxService.append': ['登记一条待发送的订单事件', '先把领域事件写入数据库 Outbox，等待后续投递。这里不是 Kafka 消费完成，也不证明仓库已经收到消息。'],
  'OutboxMapper.insert': ['把待发送事件写入数据库', '保存事件内容和待发送状态；与业务事务一起提交后，后续投递器才可可靠读取。'],
  'OrderService.get': ['核对归属并组装订单详情', '读取订单、检查当前用户能否访问，再读取明细并组合返回。'],
  'OrderMapper.findByReference': ['按订单编号读取订单', '用订单 ID 或订单号查询订单主记录。'],
  'OrderPageService.page': ['组装这一页订单', '校验分页位置，查询一页订单，再批量读取这些订单的明细，按订单 ID 分组装回。'],
  'OrderMapper.findPage': ['按游标读取下一页订单', '根据当前用户、筛选条件和上一页位置，按创建时间与 ID 倒序查询。'],
  'OrderMapper.findItemsForOrders': ['一次读取本页所有商品明细', '用本页订单 ID 集合查询明细，避免每个订单各发一次查询。'],
}

export function describeEvent(e: LearningEvent) {
  const copy = details[symbolOf(e)] ?? learningCatalog[symbolOf(e)]
  if (copy) return { title: copy[0], description: copy[1] }
  if (e.kind === 'cache' && e.name === '商品详情 l1') return { title: '读取 Java 内存里的商品缓存', description: '先查看当前 Java 进程的 Caffeine 缓存。命中后可直接复用商品详情，不必为这次缓存读取访问 Redis 或数据库。' }
  if (e.kind === 'cache' && e.name === '商品详情 l2') return { title: '读取 Redis 里的商品缓存', description: '本机缓存没有可用条目后，继续读取 Redis。读取到商品详情时会填回本机缓存；没读到时才继续后续处理。' }
  if (e.kind === 'rate_limit') return { title: e.name.includes('write-user') ? '检查当前用户的请求频率' : e.name.includes('write-ip') ? '检查当前 IP 的请求频率' : '检查请求频率', description: '根据本次限流记录判断能否继续；通过限流不代表订单已经创建。' }
  const copyFromKind = eventExplanation(e)
  if (copyFromKind.title === '执行业务处理' || copyFromKind.title === '访问业务数据')
    return { title: '待补充讲解的调用', description: '这条调用有执行记录，但还没有经过核对的中文讲解。展开代码可查看原始记录；不能据此猜测它做了什么。' }
  return copyFromKind
}

/** Only recorded public fields are narrated. No model, extra request or inferred business success. */
export function explainObserved(e: LearningEvent): string[] {
  const input = record(e.input), request = record(input.request), output = record(e.output)
  const facts: string[] = []
  if (Array.isArray(request.items)) {
    for (const raw of request.items.slice(0, 5)) {
      const item = record(raw), id = publicId(item.itemId), quantity = number(item.quantity), price = money(item.expectedUnitPriceMinor)
      if (id && quantity != null) facts.push(`你提交的商品编号是 ${id}，数量 ${quantity} 件${price == null ? '。' : `，确认时的单价是 ${price} 元。`}`)
    }
    const payable = money(request.expectedPayableMinor)
    if (payable != null) facts.push(`你确认的应付金额是 ${payable} 元；这是请求中的预期金额，不是支付回执。`)
  }
  const symbol = symbolOf(e)
  if (symbol === 'OrderMapper.findByIdempotencyKey' && e.outcome === 'executed' && e.affectedRows === 0)
    facts.push('本次没有查到相同幂等键的旧订单；后续是否建单，要继续看写入与事务结果。')
  if (symbol === 'CouponService.consume' && e.outcome === 'returned' && e.output === 0)
    facts.push('本次返回的优惠金额是 0 分；仅凭这个结果，不能断言你没有选择优惠券。')
  if (symbol === 'InventoryMapper.reserve' && e.outcome === 'executed' && e.affectedRows != null)
    facts.push(e.affectedRows === 1 ? '条件更新命中，已执行库存预占写入；能否保留还要看事务最终提交还是回滚。' : '条件更新没有正常命中一条库存记录；不能认定预占成功。')
  if (e.outcome === 'executed' && e.affectedRows === 1 && ['OrderMapper.insertOrder', 'OrderMapper.insertItem', 'InventoryMapper.insertReservation', 'OutboxMapper.insert'].includes(symbol))
    facts.push(`本次已执行“${describeEvent(e).title}”这项写入；是否最终保存，要看所属事务的提交结果。`)
  const quantity = number(request.quantity ?? input.quantity)
  const itemId = publicId(request.itemId ?? input.itemId ?? input.productId
    ?? (/^(Product|LocalOffer)/.test(symbol) ? input.id : null))
  const itemType = request.itemType ?? input.itemType ?? input.rawItemType
  const itemLabel = itemType === 'LOCAL_DEAL' ? '到店消费商品' : '商品'
  if (quantity != null && itemId && !Array.isArray(request.items)) facts.push(`本次处理${itemLabel} ${itemId}，数量 ${quantity} 件。`)
  else if (itemId) facts.push(`本次涉及的${itemLabel}编号是 ${itemId}。`)
  // An arbitrary .id is not necessarily a product. Narrate only fields with known meaning.
  const orderId = publicId(input.orderId ?? input.orderReference
    ?? (symbol === 'OrderMapper.findByReference' ? input.reference : null))
  if (orderId) facts.push(`本次涉及的订单编号是 ${orderId}。`)
  const refundId = publicId(input.refundId ?? (/^PartialRefund/.test(symbol) ? input.id : null))
  if (refundId) facts.push(`本次涉及的退款编号是 ${refundId}。`)
  const campaignId = publicId(input.campaignId)
  if (campaignId) facts.push(`本次涉及的抢购活动编号是 ${campaignId}。`)
  if (symbol === 'OrderService.preview' && e.outcome === 'returned') facts.push('本次预览方法正常返回；这个方法不会创建订单或预占库存。确认下单时仍需重新核验。')
  if (symbol === 'CouponService.quote' && e.outcome === 'returned' && number(output.discountMinor) != null)
    facts.push(`本次试算优惠为 ${money(output.discountMinor)} 元；试算不会把优惠券标为已使用。`)
  if (e.kind === 'cache') facts.push(e.outcome === 'hit' ? '这一次命中了缓存，这个缓存读取步骤不需要重新查询数据库；其他调用是否查询数据库要分别看记录。' : e.outcome === 'miss' ? '这一次没有命中缓存；需要继续看后续记录，不能把缓存未命中解释成商品不存在。' : `本次缓存结果：${outcomeExplanation(e.outcome)}。`)
  if (e.kind === 'rate_limit') facts.push(e.outcome === 'allowed' ? '这次请求未超过该项频率限制，可以继续；后面的商品与库存校验还未由这条记录证明。' : `本次频率检查结果：${outcomeExplanation(e.outcome)}。`)
  if (e.kind === 'transaction') facts.push(e.outcome === 'committed' ? '本次记录到事务提交；这表示该事务的数据库修改被提交，不代表消息已消费或已经支付。' : e.outcome === 'rolled_back' ? '本次记录到事务回滚；同一事务内此前执行的写入不会作为成功结果保留。' : '尚未取得明确的事务提交结果，不能把此前写入当作最终成功。')
  if (output.status === 'PENDING_PAYMENT') facts.push('返回的订单状态是“待支付”，还没有完成付款。')
  if (output.type === 'ResponseEntity') facts.push(symbol.startsWith('Product')
    ? '这里只记录了响应包装类型，没有保存完整商品内容；请结合商品读取与缓存记录查看实际处理。'
    : /^(CartOrder|Order)/.test(symbol)
      ? '这里只记录了 HTTP 返回容器的类型，没有记录完整订单内容；它不能作为支付成功的依据。'
      : '这里只记录了响应包装类型，没有保存完整业务结果；不能仅凭这个类型判断操作成功。')
  if (e.outcome.startsWith('threw:') || e.outcome === 'failed') facts.push('这个调用抛出了异常。上面是代码职责说明，不表示其中所有步骤都执行成功。')
  if (!facts.length) facts.push(`“${describeEvent(e).title}”${outcomeExplanation(e.outcome)}。记录没有提供可讲解的具体返回字段；上方说明的是代码职责，不是对缺失结果的猜测。`)
  return facts
}

const phases: Record<string, [string, string]> = {
  entry: ['接收并检查下单请求', '先检查请求频率，再把你确认的清单交给订单服务。'],
  duplicate: ['检查是否重复下单', '同一次请求应返回同一张订单，不能因为重复点击而多建单。'],
  quote: ['核对商品、价格和库存', '以后台商品报价和库存配置核验请求，而不是直接相信页面金额。'],
  discount: ['处理优惠并核对应付金额', '按实际优惠计算应付金额；没有优惠与没有调用优惠处理不是一回事。'],
  write: ['保存订单并预占库存', '先写主订单，再按库存记录顺序预占，随后保存商品明细。失败时由事务保护这些写入。'],
  notice: ['登记待发送的订单事件', '把事件留在数据库中等待异步投递，而不是在这里就完成发货。'],
  read: ['读取订单作为返回内容', '把刚处理的订单和商品明细组装给调用方；重复请求也可能返回已有订单。'],
  transaction: ['确认数据库事务结果', '最后区分提交和回滚。已执行某条写入，并不自动意味着最终保存成功。'],
  other: ['查看其他已记录处理', '这些记录尚未归入明确的学习阶段，保留源码与结果供核对。'],
}
function state(events: LearningEvent[]): LearningStep['state'] {
  if (events.some(e => /^(threw:|failed$|rolled_back$|rejected$|exception$)/.test(e.outcome))) return 'failed'
  return events.some(e => ['running', 'unknown', 'unavailable'].includes(e.outcome)) ? 'pending' : 'observed'
}
export function learningFlow(events: LearningEvent[]): { steps: LearningStep[]; cart: boolean } {
  const cart = events.some(e => /^CartOrder(Controller|Service)\.create$/.test(symbolOf(e)))
  const visible = events.filter(e => !['http', 'security'].includes(e.kind))
  if (!cart) {
    const steps: LearningStep[] = []
    for (const e of visible.filter(e => e.kind !== 'transaction').concat(visible.filter(e => e.kind === 'transaction'))) {
      const copy = describeEvent(e)
      // Never merge unrelated calls merely because their human labels match.
      const groupKey = e.kind === 'method' || e.kind === 'sql' ? symbolOf(e) : `${e.kind}:${e.name}`
      let step = steps.find(s => (s.events[0].kind === 'method' || s.events[0].kind === 'sql'
        ? symbolOf(s.events[0]) : `${s.events[0].kind}:${s.events[0].name}`) === groupKey)
      if (!step) { step = { id: `s${steps.length}`, title: copy.title, why: copy.description, events: [], state: 'observed' }; steps.push(step) }
      step.events.push(e); step.state = state(step.events)
    }
    return { steps, cart }
  }
  const byId = new Map(events.filter(e => e.id).map(e => [e.id!, e]))
  const inCall = (e: LearningEvent, pattern: RegExp) => {
    const seen = new Set<LearningEvent>(); let current: LearningEvent | undefined = e
    while (current && !seen.has(current)) { if (pattern.test(symbolOf(current))) return true; seen.add(current); current = current.parentId ? byId.get(current.parentId) : undefined }
    return false
  }
  const groups = new Map<string, LearningEvent[]>()
  for (const e of visible) {
    const symbol = symbolOf(e)
    let phase = 'other'
    if (e.kind === 'transaction') phase = 'transaction'
    else if (inCall(e, /^(OrderService\.get|OrderMapper\.(findByReference|findItems))$/)) phase = 'read'
    else if (inCall(e, /^Outbox(Service|Mapper)\./)) phase = 'notice'
    else if (inCall(e, /^(InventoryService\.reserve|InventoryMapper\.(reserve|insertReservation|findReservation)|OrderMapper\.(insertOrder|insertItem))$/)) phase = 'write'
    else if (inCall(e, /^Coupon(Service|Mapper)\./)) phase = 'discount'
    else if (symbol === 'OrderMapper.findByIdempotencyKey') phase = 'duplicate'
    else if (/^(Product|LocalOffer|Inventory)/.test(symbol)) phase = 'quote'
    else if (e.kind === 'rate_limit' || /^CartOrder/.test(symbol)) phase = 'entry'
    groups.set(phase, [...(groups.get(phase) ?? []), e])
  }
  return { cart, steps: Object.entries(phases).filter(([key]) => groups.has(key)).map(([key, [title, why]], i) => ({
    id: `s${i}`, title, why, events: groups.get(key)!, state: state(groups.get(key)!), decision: key === 'duplicate',
  })) }
}

/** Mermaid receives fixed editorial labels only, never trace names, raw parameters or code. */
export function flowDefinition(steps: LearningStep[]) {
  const shortTitles: Record<string, string> = {
    '接收并检查下单请求':'接收下单请求', '检查是否重复下单':'是否重复下单', '核对商品、价格和库存':'核对商品与库存',
    '处理优惠并核对应付金额':'计算优惠与应付', '保存订单并预占库存':'写订单、占库存', '登记待发送的订单事件':'登记待发送事件',
    '读取订单作为返回内容':'读取订单详情', '确认数据库事务结果':'确认事务结果',
  }
  const lines = ['flowchart TB', 'classDef normal fill:#ffffff,stroke:#d6ddd8,color:#243c32,rx:12,ry:12;',
    'classDef failed fill:#fff1f0,stroke:#d75848,color:#8e2c22;', 'classDef pending fill:#fff7e7,stroke:#c8942f,color:#765517;']
  steps.forEach((s, i) => {
    const label = `${i + 1} · ${shortTitles[s.title] ?? s.title}`.replace(/["<>\[\]{}\\\n]/g, '')
    lines.push(s.decision ? `${s.id}{"${label}"}` : `${s.id}["${label}"]`)
    lines.push(`class ${s.id} ${s.state === 'observed' ? 'normal' : s.state};`)
    if (i) {
      const previous = steps[i - 1]
      const lookup = previous.decision && previous.events.find(e => symbolOf(e) === 'OrderMapper.findByIdempotencyKey' && e.outcome === 'executed')
      const label = lookup && lookup.affectedRows != null ? lookup.affectedRows === 0 ? '未查到旧订单' : '查到旧订单' : ''
      lines.push(`${previous.id} -->${label ? `|${label}|` : ''} ${s.id}`)
    }
  })
  return lines.join('\n')
}
