import { useEffect, useState } from 'react'
import { api, errorMessage } from './lib/api'
import { parseOrder, money } from './lib/orders'
import type { Order } from './lib/orders'
import { isProductId } from './lib/identity'
import type { ProductId } from './lib/identity'
import { CustomerAfterSales } from './CustomerAfterSales'

type Balance = { itemId: ProductId; quantity: number; refundedQuantity: number; refundedMinor: number }
type Data = { order: Order; balance: Balance[]; remainingSeconds?: number | null; fulfillment: null | { status: string; trackingNo: string | null } }
const labels: Record<string, string> = { WAITING_PAYMENT: '等待支付', READY: '待出库', DISPATCHING: '出库处理中',
  UNKNOWN: '正在核对出库结果', SHIPPED: '已出库', RECEIVED: '已收货', REFUND_HOLD: '退款处理中，暂停出库',
  CANCELLED: '履约已取消', NEEDS_REVIEW: '需人工核对' }

export function OrderAfterSales({ id, csrf, onPreview }: {
  id: string; csrf: string; onPreview: (path: string, body: object) => void
}) {
  const [data, setData] = useState<Data | null>(null), [error, setError] = useState('')
  const [quantities, setQuantities] = useState<Record<string, number>>({})
  const [reason, setReason] = useState('暂时不需要了'), [revision, setRevision] = useState(0)
  const [remaining, setRemaining] = useState<number | null>(null)
  useEffect(() => {
    const timer = setInterval(() => setRemaining(n => n == null ? null : Math.max(0, n - 1)), 1000)
    const refresh = setInterval(() => setRevision(v => v + 1), 10000)
    return () => { clearInterval(timer); clearInterval(refresh) }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    api(`/workspace/orders/${encodeURIComponent(id)}/after-sales`, {
      observation: { label: '加载或刷新履约与退款状态', trigger: 'effect', code: 'frontend/src/OrderAfterSales.tsx → useEffect' },
      signal: controller.signal, headers: { 'X-CSRF-Token': csrf },
    }).then(value => {
      const d = value as Data
      const order = parseOrder(d.order)
      if (order.id !== id || !Array.isArray(d.balance) || !d.balance.every(b =>
        isProductId(b.itemId) && Number.isSafeInteger(b.quantity) &&
        Number.isSafeInteger(b.refundedQuantity) && b.refundedQuantity >= 0 && b.refundedQuantity <= b.quantity))
        throw new Error('售后数据不完整')
      if (d.fulfillment && typeof d.fulfillment.status !== 'string') throw new Error('履约数据不完整')
      if (!controller.signal.aborted) { setData({ ...d, order }); setRemaining(d.remainingSeconds ?? null); setError('') }
    }).catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    return () => controller.abort()
  }, [id, csrf, revision])
  const items = data?.balance.filter(b => (quantities[b.itemId] ?? 0) > 0)
    .map(b => ({ itemId: b.itemId, quantity: quantities[b.itemId] })) ?? []
  const refundable = data?.order.status === 'PAID' && data.balance.length > 0 &&
    ['WAITING_PAYMENT', 'READY'].includes(data.fulfillment?.status ?? '')
  return <section className="after-sales">
    <h3>履约与售后</h3>
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p>正在核对订单…</p>}
    {data && <>
      {remaining != null && <p>{remaining > 0 ? `支付剩余 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒` : '支付期限已到，正在核对关单结果；请勿重复支付。'}</p>}
      <p>{data.fulfillment ? labels[data.fulfillment.status] ?? data.fulfillment.status : '此历史订单没有履约记录'}</p>
      {data.fulfillment?.trackingNo && <p>模拟运单：{data.fulfillment.trackingNo}</p>}
      {data.order.status === 'PENDING_PAYMENT' && <button className="button secondary"
        onClick={() => onPreview('/cancel-preview', { orderId: id })}>预览取消订单</button>}
      {data.balance.map(b => <div className="refund-line" key={b.itemId}>
        <span>{data.order.items.find(i => i.itemId === b.itemId)?.titleSnapshot ?? `商品 ${b.itemId}`}</span>
        <small>已退 {b.refundedQuantity} 件 · {money(b.refundedMinor)}</small>
        {refundable && <label>退款数量 <input type="number" min={0} max={b.quantity - b.refundedQuantity}
          value={quantities[b.itemId] ?? 0} onChange={e => setQuantities(old => ({ ...old,
            [b.itemId]: Math.max(0, Math.min(b.quantity - b.refundedQuantity, Math.trunc(Number(e.target.value)) || 0)),
          }))} /></label>}
      </div>)}
      {refundable && <>
        <label>退款原因 <input value={reason} maxLength={255} onChange={e => setReason(e.target.value)} /></label>
        <button className="button secondary" disabled={!items.length || !reason.trim()}
          onClick={() => onPreview('/refund-preview', { orderId: id, items, reason: reason.trim() })}>预览退款金额</button>
      </>}
      {data.order.status === 'PAID' && !refundable && <p className="muted">按数量退款仅限新订单尚未派发的商品；已派发或结果未明时不能自动退款。</p>}
    </>}
    <button className="text-button" onClick={() => setRevision(v => v + 1)}>刷新履约与退款状态</button>
    {data && <CustomerAfterSales key={id} order={data.order} balance={data.balance} csrf={csrf} received={data.fulfillment?.status === 'RECEIVED'} />}
  </section>
}
