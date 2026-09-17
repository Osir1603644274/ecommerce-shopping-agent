import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './lib/api'
import { money } from './lib/orders'
import { campaignState } from './lib/benefits'
import type { Campaign } from './lib/benefits'
type Coupon = { id: string; templateId: string; status: string }
type Purchase = { id: string; campaignId: string; status: string; amountMinor: number }
type Data = { campaigns: Campaign[]; coupons: Coupon[]; purchases: Purchase[] }
const states: Record<string, string> = { ACTIVE: '进行中', DRAFT: '未开始', ENDED: '已结束',
  UNUSED: '未使用', AVAILABLE: '可用', USED: '已使用', LOCKED: '已锁定', EXPIRED: '已过期',
  ACCEPTED: '已受理，等待处理', QUEUED: '已受理，排队落单', PENDING: '处理中', CREATED: '已创建', PAID: '已支付', CANCELLED: '已取消' }
export function Benefits({ csrf, loggedIn, onLogin }: { csrf: string; loggedIn: boolean; onLogin: () => void }) {
  const [data, setData] = useState<Data | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [revision, setRevision] = useState(0), [selected, setSelected] = useState<Campaign | null>(null)
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState(''), [now, setNow] = useState(Date.now())
  const confirmRef = useRef<HTMLElement>(null)
  const ownerGeneration = useRef(0)
  const [pendingId, setPendingId] = useState<string | null>(null)
  useEffect(() => {
    ownerGeneration.current += 1
    setData(null); setError(''); setLoadError(''); setNotice(''); setSelected(null); setPendingId(null)
    busyRef.current = false; setBusy(false)
    return () => { ownerGeneration.current += 1 }
  }, [csrf, loggedIn])
  useEffect(() => {
    if (!pendingId || data?.purchases.some(p => p.id === pendingId)) return
    let count = 0
    const timer = setInterval(() => { setRevision(n => n + 1); if (++count >= 10) clearInterval(timer) }, 3000)
    return () => clearInterval(timer)
  }, [pendingId, !!data?.purchases.some(p => p.id === pendingId)])
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [])
  useEffect(() => { if (selected) { confirmRef.current?.scrollIntoView({ block: 'center' }); confirmRef.current?.focus() } }, [selected])
  const busyRef = useRef(false)
  useEffect(() => {
    if (!loggedIn) return
    const controller = new AbortController()
    api('/workspace/benefits', { observation: { label: '加载或刷新优惠与抢购', trigger: 'effect', code: 'frontend/src/Benefits.tsx → useEffect' }, signal: controller.signal, headers: { 'X-CSRF-Token': csrf } })
      .then(value => { if (!controller.signal.aborted) { setData(value as Data); setLoadError('') } })
      .catch(e => { if (!controller.signal.aborted) setLoadError(errorMessage(e)) })
    return () => controller.abort()
  }, [csrf, loggedIn, revision])
  async function purchase() {
    if (!selected || busyRef.current) return
    if (!campaignState(selected).available) { setError('活动已结束或当前不可参加，请刷新活动。'); setSelected(null); return }
    busyRef.current = true; setBusy(true); setError(''); setNotice('')
    const generation = ownerGeneration.current
    try {
      const result = await api(`/workspace/benefits/${encodeURIComponent(selected.id)}/purchase`, {
        observation: { label: '确认参加抢购', trigger: 'user', code: 'frontend/src/Benefits.tsx → purchase' },
        method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ confirmation: '确认参加抢购' }),
      }) as { orderId: string; status: string }
      if (generation !== ownerGeneration.current) return
      setNotice(`后台返回：${states[result.status] ?? result.status}，受理单号 ${result.orderId}。受理不等于支付或履约完成。`)
      setPendingId(['ACCEPTED','QUEUED','PENDING'].includes(result.status) ? result.orderId : null)
    } catch (e) {
      if (generation !== ownerGeneration.current) return
      setError(errorMessage(e) + '。若无明确回执，请先刷新我的抢购记录核验，不要连续重试。')
    } finally {
      if (generation === ownerGeneration.current) {
        busyRef.current = false; setBusy(false); setSelected(null); setRevision(n => n + 1)
      }
    }
  }
  if (!loggedIn) return <div className="shop-empty"><h2>优惠与抢购</h2><button className="button primary" onClick={onLogin}>登录查看</button></div>
  return <section className="shop-orders benefits-page">
    <div className="shop-section-heading"><h1>优惠与抢购</h1><button className="button secondary" disabled={busy} onClick={() => setRevision(n => n + 1)}>刷新我的权益与抢购</button></div>
    {error && <p role="alert">{error}</p>}{loadError && <p role="alert">刷新失败：{loadError}</p>}{notice && <p role="status">{notice}</p>}
    <h2>我的优惠券</h2>
    <p>这里只展示已有权益；当前没有可领取模板列表，不提供虚构的领券入口。</p>
    {data && !data.coupons.length && <p>当前没有优惠券。</p>}
    <div className="benefit-grid">{data?.coupons.map(c => <article key={c.id}><strong>优惠券模板 {c.templateId}</strong><p>{states[c.status] ?? c.status}</p><small>券号 {c.id}</small></article>)}</div>
    <h2>限时抢购</h2>
    <p>库存为查询时快照，以后台受理结果为准；每个活动每位用户限一单。</p>
    {data && !data.campaigns.length && <p>当前没有配置活动。</p>}
    <div className="benefit-grid">{data?.campaigns.map(c => <article key={c.id}>
      <h3>{c.title}</h3><strong>{money(c.salePriceMinor)}</strong><p>{campaignState(c, now).label} · 查询时库存 {c.availableStock}</p>
      <button className="button primary" disabled={busy || !campaignState(c, now).available} onClick={() => { setSelected(c); setNotice(''); setError('') }}>参加抢购</button>
    </article>)}</div>
    {selected && <section ref={confirmRef} tabIndex={-1} className="benefit-confirm" role="region" aria-label="确认抢购">
      <h3>确认参加：{selected.title}</h3><p>活动价 {money(selected.salePriceMinor)}。确认会提交真实的本地抢购受理请求，不会自动支付。</p>
      <button className="button primary" disabled={busy} onClick={() => void purchase()}>{busy ? '提交中…' : '确认参加抢购'}</button>{' '}
      <button className="button secondary" disabled={busy} onClick={() => setSelected(null)}>暂不参加</button>
    </section>}
    <h2>我的抢购记录</h2>
    {pendingId && !data?.purchases.some(p => p.id === pendingId) && <p role="status">受理单 {pendingId} 正在等待落单；页面会短时自动回查，也可以手动刷新。不会重复提交抢购。</p>}
    {data && !data.purchases.length && <p>暂无已落库的抢购订单。刚提交后请稍后刷新；无记录不能直接认定受理失败。</p>}
    <div className="benefit-grid">{data?.purchases.map(p => <article key={p.id}><strong>{states[p.status] ?? p.status}</strong><p>活动 {p.campaignId} · {money(p.amountMinor)}</p><small>订单 {p.id}</small></article>)}</div>
    <p>取消、部分退款及模拟物流在“我的订单 → 订单详情”中查看。后台消息消费尚未接入本次请求详情，不把已受理显示为已履约。</p>
  </section>
}
