import { useEffect, useRef, useState } from 'react'
import { CustomerSupportTickets } from './CustomerSupportTickets'
import { CustomerSupportChat } from './CustomerSupportChat'
import { api, errorMessage } from './lib/api'
import { money } from './lib/orders'
import type { Order } from './lib/orders'
import type { ProductId } from './lib/identity'
import { parseSupportCase, parseSupportPreview, specificationLabel, supportPhases, supportRequestKey, supportTypes } from './lib/support'
import type { SupportCase, SupportPreview, SupportType } from './lib/support'

type Balance = { itemId: ProductId; quantity: number; refundedQuantity: number }
type Card = SupportPreview & { label: string; conversion: boolean; quantity: number; title: string; specification: string | null }

export function CustomerAfterSales({ order, balance, csrf, received }: { order: Order; balance: Balance[]; csrf: string; received: boolean }) {
  const [cases, setCases] = useState<SupportCase[]>([]), [revision, setRevision] = useState(0)
  const [item, setItem] = useState(String(order.items[0]?.itemId ?? '')), [quantity, setQuantity] = useState(1)
  const [type, setType] = useState<SupportType>('RETURN_REFUND'), [reason, setReason] = useState('')
  const [card, setCard] = useState<Card | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const [notice, setNotice] = useState(''), [tracking, setTracking] = useState<Record<string, string>>({})
  const acting = useRef(false)
  const root = '/workspace/support'
  useEffect(() => {
    const controller = new AbortController()
    api(`${root}/orders/${encodeURIComponent(order.id)}/cases`, { signal: controller.signal, headers: { 'X-CSRF-Token': csrf } })
      .then(value => { if (!Array.isArray(value)) throw new Error('售后列表格式不正确'); const rows = value.map(parseSupportCase)
        if (rows.some(row => row.orderId !== order.id)) throw new Error('售后订单不匹配')
        if (!controller.signal.aborted) setCases(rows) })
      .catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    const timer = setTimeout(() => setRevision(v => v + 1), 10000)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [order.id, csrf, revision])
  const selected = order.items.find(row => String(row.itemId) === item)
  const line = balance.find(row => String(row.itemId) === item)
  const exchanged = cases.filter(row => String(row.itemId) === item && row.type === 'EXCHANGE' && row.phase === 'COMPLETED')
    .reduce((sum, row) => sum + row.quantity, 0)
  const remaining = Math.max(0, (line?.quantity ?? 0) - (line?.refundedQuantity ?? 0) - exchanged)
  const active = cases.some(row => !['COMPLETED', 'REJECTED', 'CANCELLED'].includes(row.phase))
  async function post(path: string, body: object, keyed = true) {
    return api(root + path, { method: 'POST', headers: { 'X-CSRF-Token': csrf,
      ...(keyed ? { 'Idempotency-Key': await supportRequestKey(order.id, path, body) } : {}) }, body: JSON.stringify(body) })
  }
  async function act(work: () => Promise<void>) {
    if (acting.current) return
    acting.current = true
    setBusy(true); setError(''); setNotice('')
    try { await work(); setRevision(v => v + 1) } catch (e) { setError(errorMessage(e)) } finally { acting.current = false; setBusy(false) }
  }
  const refresh = () => setRevision(v => v + 1)
  return <section className="customer-after-sales" aria-label="客服售后中心">
    <h3>客服售后中心</h3>
    <CustomerSupportChat orderId={order.id} csrf={csrf} onPreview={setCard} />
    {error && <p role="alert">{error}。如请求中断，请先刷新进度，再用原操作重试。</p>}
    {notice && <p role="status">{notice}</p>}
    <p className="muted">签收后七天内申请，具体资格和金额由订单记录核对。仅退款需审核，退货与换货需仓库验收。</p>
    {received && !active && <fieldset disabled={busy}>
      <legend>申请售后</legend>
      <label>商品 <select value={item} onChange={e => { setItem(e.target.value); setQuantity(1); setCard(null) }}>
        {order.items.filter(row => row.itemType === 'PRODUCT').map(row => <option key={String(row.itemId)} value={String(row.itemId)}>{row.titleSnapshot}</option>)}
      </select></label>
      <label>办理方式 <select value={type} onChange={e => { setType(e.target.value as SupportType); setCard(null) }}>
        {Object.entries(supportTypes).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>数量 <input type="number" min={1} max={remaining || 1} value={quantity}
        onChange={e => { setQuantity(Math.max(1, Math.min(remaining || 1, Math.trunc(Number(e.target.value)) || 1))); setCard(null) }} /></label>
      <label>原因 <textarea value={reason} maxLength={1000} onChange={e => { setReason(e.target.value); setCard(null) }} /></label>
      {type === 'EXCHANGE' && <p>只换原商品的相同规格，不改型号、颜色或容量。</p>}
      <button className="button secondary" disabled={!remaining || !reason.trim() || !selected} onClick={() => void act(async () => {
        const raw = await post('/preview', { orderId: order.id, itemId: selected!.itemId, quantity, type, reason: reason.trim() }, false)
        const preview = parseSupportPreview(raw)
        setCard({ ...preview, label: supportTypes[type], conversion: false, quantity, title: selected!.titleSnapshot,
          specification: preview.specification ?? null })
      })}>预览申请</button>
    </fieldset>}
    {!received && <p>当前还未签收；物流、支付或其他疑问可提交下方工单。</p>}
    {card && <section aria-label="售后确认卡" className="support-confirmation">
      <h4>请核对后确认：{card.label}</h4>
      <p>{card.title} · {card.quantity} 件 · {specificationLabel(card.specification)}</p>
      <p>{card.label === supportTypes.EXCHANGE ? '原商品实付参考金额' : '预计退款金额'}：{money(card.amountMinor, card.currency)}</p>
      <p>确认卡五分钟内有效。申请成功不表示退款到账或补发完成。</p>
      <button className="button" disabled={busy} onClick={() => void act(async () => {
        await post(card.conversion ? '/conversion-confirm' : '/confirm', { previewId: card.previewId })
        setCard(null); setNotice('申请已受理，请查看下方办理进度。')
      })}>确认{card.label}</button>
      <button className="text-button" disabled={busy} onClick={() => setCard(null)}>返回修改</button>
    </section>}
    {cases.map(row => <article key={row.id} className="support-case">
      <h4>{supportTypes[row.type]} · {supportPhases[row.phase]}</h4>
      <p>{order.items.find(i => String(i.itemId) === String(row.itemId))?.titleSnapshot ?? '订单商品'} · {row.quantity} 件</p>
      <small>售后编号：{row.id}</small>
      {['AWAITING_REVIEW', 'AWAITING_RETURN'].includes(row.phase) && <button className="text-button" disabled={busy}
        onClick={() => void act(async () => { await post(`/cases/${row.id}/cancel`, { expectedVersion: row.version }); setNotice('申请已撤销。') })}>撤销申请</button>}
      {row.phase === 'AWAITING_RETURN' && <div>
        <label>寄回单号 <input value={tracking[row.id] ?? ''} onChange={e => setTracking(v => ({ ...v, [row.id]: e.target.value }))} maxLength={128} /></label>
        <button className="button secondary" disabled={busy || !/^[A-Za-z0-9_-]{3,128}$/.test(tracking[row.id] ?? '')}
          onClick={() => void act(async () => { await post(`/cases/${row.id}/return-shipment`, { expectedVersion: row.version, trackingNo: tracking[row.id] }); setNotice('已登记寄回，等待仓库收货与验收。') })}>登记寄回</button>
      </div>}
      {row.phase === 'WAITING_CHOICE' && <div>
        <button className="button secondary" disabled={busy} onClick={() => void act(async () => { await post(`/cases/${row.id}/wait-stock`, { expectedVersion: row.version }); setNotice('已选择继续等待库存。') })}>继续等待</button>
        <button className="button secondary" disabled={busy} onClick={() => void act(async () => {
          const value = await post(`/cases/${row.id}/conversion-preview`, {}, false)
          setCard({ ...parseSupportPreview(value), label: '转为退货退款', conversion: true, quantity: row.quantity,
            title: order.items.find(i => String(i.itemId) === String(row.itemId))?.titleSnapshot ?? '订单商品', specification: row.specification })
        })}>预览转退款</button>
      </div>}
    </article>)}
    <button className="text-button" disabled={busy} onClick={refresh}>刷新售后进度</button>
    <CustomerSupportTickets orderId={order.id} cases={cases} csrf={csrf} />
  </section>
}
