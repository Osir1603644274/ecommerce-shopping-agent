import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './lib/api'
import { parseSupportTicket, parseTicketEvent, supportRequestKey, supportTypes, ticketCategories, ticketStatuses } from './lib/support'
import type { SupportCase, SupportTicket, TicketEvent } from './lib/support'

const root = '/workspace/support'
export function CustomerSupportTickets({ orderId, cases, csrf }: { orderId: string; cases: SupportCase[]; csrf: string }) {
  const [tickets, setTickets] = useState<SupportTicket[]>([]), [offset, setOffset] = useState(0), [hasMore, setHasMore] = useState(false)
  const [revision, setRevision] = useState(0), [category, setCategory] = useState<keyof typeof ticketCategories>('DELIVERY_DELAY')
  const [caseId, setCaseId] = useState(''), [summary, setSummary] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false), [selected, setSelected] = useState(''), [events, setEvents] = useState<TicketEvent[]>([])
  const [reply, setReply] = useState(''), acting = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    api(`${root}/tickets?offset=${offset}&limit=20`, { signal: controller.signal, headers: { 'X-CSRF-Token': csrf } })
      .then(value => { if (!Array.isArray(value)) throw new Error('工单列表格式不正确')
        const rows = value.map(parseSupportTicket)
        if (!controller.signal.aborted) { setTickets(rows); setHasMore(rows.length === 20) } })
      .catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    return () => controller.abort()
  }, [offset, csrf, revision])
  useEffect(() => {
    setEvents([])
    if (!selected) return
    const controller = new AbortController()
    api(`${root}/tickets/${encodeURIComponent(selected)}/events`, { signal: controller.signal, headers: { 'X-CSRF-Token': csrf } })
      .then(value => { if (!Array.isArray(value)) throw new Error('工单记录格式不正确')
        const rows = value.map(parseTicketEvent); if (!controller.signal.aborted) setEvents(rows) })
      .catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    return () => controller.abort()
  }, [selected, csrf, revision])
  async function post(path: string, body: object) {
    return api(root + path, { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': await supportRequestKey(orderId, path, body) }, body: JSON.stringify(body) })
  }
  async function act(work: () => Promise<void>) {
    if (acting.current) return
    acting.current = true; setBusy(true); setError(''); setNotice('')
    try { await work(); setRevision(v => v + 1) } catch (e) { setError(errorMessage(e)) }
    finally { acting.current = false; setBusy(false) }
  }
  return <section className="support-tickets" aria-label="客服工单">
    <h3>客服工单</h3>
    <p className="muted">需要催办、核实或申诉时可留言。工单答复或关闭不代表退款到账、物流签收或售后办理完成。</p>
    {error && <p role="alert">{error}。请求中断时请先刷新记录，再重试原操作。</p>}
    {notice && <p role="status">{notice}</p>}
    <fieldset disabled={busy}>
      <legend>为当前订单提交工单</legend>
      <label>问题类型 <select value={category} onChange={e => setCategory(e.target.value as keyof typeof ticketCategories)}>
        {Object.entries(ticketCategories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>关联售后 <select value={caseId} onChange={e => setCaseId(e.target.value)}>
        <option value="">订单问题，无需关联售后</option>
        {cases.map(row => <option key={row.id} value={row.id}>{supportTypes[row.type]} · {row.id}</option>)}
      </select></label>
      <label>问题说明 <textarea value={summary} maxLength={1000} onChange={e => setSummary(e.target.value)} /></label>
      <button className="button secondary" disabled={!summary.trim()} onClick={() => void act(async () => {
        const ticket = parseSupportTicket(await post('/tickets', { orderId, caseId: caseId || null, category, summary: summary.trim() }))
        if (ticket.orderId !== orderId) throw new Error('工单订单不匹配')
        setSummary(''); setOffset(0); setSelected(ticket.id); setNotice(`工单已登记：${ticket.id}`)
      })}>提交工单</button>
    </fieldset>
    <h4>我的工单（全部订单）</h4>
    {tickets.length === 0 && <p>当前页暂无工单。</p>}
    {tickets.map(ticket => <article className="support-case" key={ticket.id}>
      <h4>{ticketCategories[ticket.category]} · {ticketStatuses[ticket.status]}</h4>
      <p>{ticket.summary}</p><small>订单：{ticket.orderId} · 工单：{ticket.id}</small>
      <button className="text-button" onClick={() => { setSelected(selected === ticket.id ? '' : ticket.id); setReply('') }}>查看沟通记录</button>
      {selected === ticket.id && <div>
        {events.map(event => <p key={event.id}><time>{new Date(event.createdAt).toLocaleString()}</time> · {event.action === 'CREATED' || event.action === 'REPLY' ? '我' : '客服'}：{event.message}</p>)}
        {ticket.status !== 'CLOSED' && <>
          <label>补充说明 <textarea value={reply} maxLength={2000} onChange={e => setReply(e.target.value)} /></label>
          <button className="button secondary" disabled={busy || !reply.trim()} onClick={() => void act(async () => {
            await post(`/tickets/${ticket.id}/reply`, { expectedVersion: ticket.version, message: reply.trim() })
            setReply(''); setNotice('补充说明已提交。')
          })}>发送补充说明</button>
        </>}
      </div>}
    </article>)}
    <button className="text-button" disabled={offset === 0 || busy} onClick={() => { setOffset(Math.max(0, offset - 20)); setSelected('') }}>上一页</button>
    <button className="text-button" disabled={!hasMore || busy} onClick={() => { setOffset(offset + 20); setSelected('') }}>下一页</button>
    <button className="text-button" disabled={busy} onClick={() => setRevision(v => v + 1)}>刷新工单</button>
  </section>
}
