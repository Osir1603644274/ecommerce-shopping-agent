export type ExplainEvent = { kind: string; name: string; outcome: string; affectedRows?: number | null }
const methods: Record<string, [string, string]> = {
  'ProductPurchaseViewController.get': ['汇总商品详情与购买条件', '复用商品详情缓存，并查询当前报价库存，在同一个接口中返回；不创建订单。'],
  'ProductOfferController.get': ['汇总购买条件', '读取商品、报价和可用库存，整理这件商品当前能否购买。'],
  'ProductMapper.findById': ['读取商品资料', '按商品编号，从数据库读取这件商品的资料。'],
  'LocalOfferService.find': ['查找商品报价', '检查本地报价功能；开启时查询这件商品的本地报价。'],
  'ProductMapper.findCommerceFacts': ['核对价格与库存', '读取商品价格、上架状态与库存，供交易核验使用。'],
  'ProductMapper.findAttributes': ['读取商品属性', '读取已经登记的商品规格和属性。'],
  'ProductMapper.findByFilters': ['筛选候选商品', '按查询条件从商品目录中寻找候选商品。'],
  'CouponMapper.findUserCoupons': ['读取我的优惠券', '查询当前用户拥有的优惠券。'],
  'FlashSaleMapper.findCampaigns': ['读取抢购活动', '从数据库读取抢购活动信息。'],
  'FlashSaleMapper.findOrdersByUser': ['读取我的抢购记录', '查询当前用户参与抢购产生的订单。'],
  'OrderMapper.findItems': ['读取订单明细', '按订单编号，查询这张订单包含的商品。'],
}
export function eventExplanation(event: ExplainEvent) {
  const symbol = event.name.replace(/ \[[^\]]+\]$/, '').split('.').slice(-2).join('.')
  const generic: Record<string, [string, string]> = {
    sql: ['访问业务数据', '执行这次业务需要的数据库操作。下方是实际语句及参数。'],
    transaction: ['确认事务结果', '观察这一组数据库操作最终提交、回滚，还是结果未明确。'],
    cache: ['读取缓存', '尝试复用已缓存的数据；未命中不代表数据库中没有数据。'],
    rate_limit: ['检查请求频率', '判断当前请求是否超过限流额度。'],
    redis_lua: ['检查抢购资格与库存', '通过 Redis 脚本检查重复购买和可用库存，返回受理结果。'],
    method: ['执行业务处理', '调用本次请求涉及的业务代码；具体方法见下方。'],
  }
  const copy = methods[symbol] ?? generic[event.kind] ?? ['处理请求', '本次操作实际记录到的一项处理。']
  return { title: copy[0], description: copy[1] }
}
export function outcomeExplanation(outcome: string) {
  const known: Record<string, string> = { returned: '已返回', executed: '已执行', committed: '已提交', rolled_back: '已回滚',
    passed: '已完成', completed: '已完成', planned: '已生成计划', step_executed: '已执行', generated: '已生成',
    tool_succeeded: '已完成', tool_failed: '执行失败', hit: '命中缓存', miss: '未命中缓存', empty: '缓存结果为空',
    allowed: '允许继续', rejected: '已拒绝', accepted: '已受理', sold_out: '已售罄', duplicate: '重复购买',
    not_ready: '库存未就绪', unknown: '结果待确认', unavailable: '暂无结果', running: '处理中',
    failed: '执行失败', cancelled: '已停止', queued_batch: '已排入批次，尚未执行', insufficient_evidence: '证据不足' }
  return known[outcome] ?? (outcome.startsWith('threw:') ? '处理时发生异常' : '查看处理结果')
}
export function requestExplanation(path: string, method = 'GET') {
  if (path === '/api/products/resolve-sources') return '把检索记录匹配到商城商品'
  if (/\/products\/[^/]+\/purchase-view$/.test(path)) return '读取商品详情与当前购买条件'
  if (/\/products\/[^/]+\/offer$/.test(path)) return '核对商品价格与库存'
  if (/\/products\/[^/]+$/.test(path)) return '读取商品详情'
  if (path.includes('coupons')) return '读取优惠券信息'
  if (path.includes('flash-sales')) return method === 'GET' ? '读取抢购信息' : '提交抢购请求'
  if (path.includes('/orders')) return method === 'GET' ? '查看订单' : '处理订单请求'
  if (path.includes('favorites')) return method === 'GET' ? '查看我的收藏' : '更新商品收藏'
  if (path.includes('benefits')) return '查看优惠与抢购'
  if (path.includes('/selection')) return '选择商品'
  if (path.includes('/products')) return '查询商品'
  if (path.includes('/payments')) return '处理支付请求'
  if (path.includes('/refund')) return '处理退款请求'
  if (path.includes('/auth')) return '核验登录信息'
  return '处理页面请求'
}
