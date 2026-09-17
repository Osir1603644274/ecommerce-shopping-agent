import { requestExplanation } from './explainExecution'

export type JourneyCall = { method: string; path: string; status: number }
export function requestJourney(calls: JourneyCall[], pagePath: string) {
  const preview = /\/preview$/.test(pagePath) && !pagePath.includes('payment')
  const notes = calls.map(call => {
    const path = call.path
    if (path === '/api/products/resolve-sources') return {
      title: '把检索记录匹配到商城商品',
      why: '按来源、来源内编号和原文校验值，查找已接入且在售的商城商品。匹配成功后还要读取当前价格和库存；这一步不会下单。',
    }
    if (/\/products\/[^/]+\/purchase-view$/.test(path)) return {
      title: '读取商品详情与当前购买条件',
      why: '一次 Java 请求返回两部分：可复用缓存的商品资料，以及从交易端核对的当前报价和库存。合并接口不代表将实时库存放入缓存。',
    }
    if (/\/products\/[^/]+\/offer$/.test(path)) return {
      title: preview ? '刷新价格与库存展示' : '核对商品价格与库存',
      why: preview ? '这条记录中 Agent 再次获取购买条件，随后 Java 预览还会重新核验：这是记录中的两次读取，不是已合并的一次校验。旧版本记录仍保留原始含义。' : '商品详情之外，单独查询当前交易报价、可用库存和是否允许购买。',
    }
    if (/\/products\/[^/]+$/.test(path)) return {
      title: preview ? '刷新选中商品的资料' : '读取商品详情',
      why: preview ? '预览入口重新组装选中商品卡片，因此再次读取商品标题等资料；这不是创建第二件商品，也不是重复下单。' : '按选中的商品编号读取标题、属性等详情；命中缓存时可以复用缓存内容。',
    }
    if (path === '/api/identity/me') return { title: '核实本次交易的登录身份', why: 'Agent 向 Java 查询当前身份，用于绑定这次交易请求的用户；不是让你再次登录。' }
    if (path === '/api/orders/preview') return { title: '试算本单金额与购买数量', why: 'Java 按商品编号、数量和用户重新核价、计算优惠，并判断库存是否足够。预览不会建单、占库存或扣款。' }
    return { title: requestExplanation(path, call.method), why: '这是当前选中页面请求下实际记录到的一次 Java 调用。展开后查看它涉及的代码和处理。' }
  })
  return { notes, explanation: preview
    ? '你点击的是一次“预览订单”。下面是实际记录的 Java 调用，不是多次下单。Java 重新核验价格和库存，预览结果用于更新展示；旧记录若含商品查询，仍按原调用展示。'
    : '下面的环节属于当前选中的同一次页面请求，不代表你点击了多次。选择一个环节，查看它的具体处理。' }
}
