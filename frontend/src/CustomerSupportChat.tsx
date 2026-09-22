import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './lib/api'
import { parseSupportPreview, supportRequestKey, ticketCategories } from './lib/support'
import type { SupportPreview } from './lib/support'

export type ChatPreview = SupportPreview & { orderId: string; label: string; conversion: boolean; quantity: number; title: string; specification: string | null }
type Draft = { orderId: string; caseId: string | null; category: keyof typeof ticketCategories; summary: string }
type ActionDraft = { orderId: string; caseId: string; action: 'cancel' | 'return_shipment' | 'wait_stock'; label: string; body: { expectedVersion: number; trackingNo?: string } }
const actionPaths = { cancel: 'cancel', return_shipment: 'return-shipment', wait_stock: 'wait-stock' }
type Turn = { requestId: string; message: string; status: 'RUNNING' | 'COMPLETED' | 'FAILED'; result: null | {
  answer: string; citations: { id: string; title?: string; text?: string; kind: string; observedAt?: string }[]; preview: ChatPreview | null; ticketDraft: Draft | null; actionDraft?: ActionDraft | null
  ticketReplyDraft?: { orderId: string; ticketId: string; body: { expectedVersion: number; message: string } } | null
} }
type Conversation = { enabled: boolean; turns: Turn[] }
function parseConversation(value: unknown, orderId: string): Conversation {
  const v = value as Conversation
  if (!v || typeof v.enabled !== 'boolean' || !Array.isArray(v.turns)) throw new Error('客服对话记录不完整')
  for (const turn of v.turns) {
    if (!turn || typeof turn.requestId !== 'string' || typeof turn.message !== 'string' || !['RUNNING', 'COMPLETED', 'FAILED'].includes(turn.status)) throw new Error('客服消息不完整')
    const result = turn.result
    if (!result) continue
    if (typeof result.answer !== 'string' || !Array.isArray(result.citations)) throw new Error('客服回答缺少依据记录')
    if (result.citations.some(citation => !citation || typeof citation.id !== 'string' || typeof citation.kind !== 'string'
      || (citation.text != null && typeof citation.text !== 'string') || (citation.title != null && typeof citation.title !== 'string'))) throw new Error('客服依据格式不正确')
    if (result.preview) {
      parseSupportPreview(result.preview)
      if (result.preview.orderId !== orderId || typeof result.preview.title !== 'string' || typeof result.preview.label !== 'string' || !Number.isSafeInteger(result.preview.quantity) || result.preview.quantity < 1) throw new Error('客服确认卡不完整')
    }
    if (result.ticketDraft && (result.ticketDraft.orderId !== orderId || typeof result.ticketDraft.summary !== 'string' || !Object.hasOwn(ticketCategories, result.ticketDraft.category))) throw new Error('工单草稿订单不匹配')
    if (result.ticketReplyDraft && (result.ticketReplyDraft.orderId !== orderId || !/^[a-zA-Z0-9_-]{1,80}$/.test(result.ticketReplyDraft.ticketId)
      || !Number.isSafeInteger(result.ticketReplyDraft.body?.expectedVersion) || result.ticketReplyDraft.body.expectedVersion < 0
      || typeof result.ticketReplyDraft.body.message !== 'string' || !result.ticketReplyDraft.body.message.trim() || result.ticketReplyDraft.body.message.length > 2000)) throw new Error('工单回复草稿不完整')
    if (result.actionDraft && (result.actionDraft.orderId !== orderId || !/^[a-zA-Z0-9_-]{1,80}$/.test(result.actionDraft.caseId) || !Object.hasOwn(actionPaths, result.actionDraft.action)
      || !Number.isSafeInteger(result.actionDraft.body?.expectedVersion) || result.actionDraft.body.expectedVersion < 0)) throw new Error('售后操作草稿不完整')
  }
  return v
}

export function CustomerSupportChat({ orderId, csrf, onPreview }: { orderId: string; csrf: string; onPreview: (card: ChatPreview) => void }) {
  const [state, setState] = useState<Conversation>({ enabled: false, turns: [] }), [revision, setRevision] = useState(0)
  const [message, setMessage] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const pending = useRef<{ message: string; requestId: string } | null>(null), acting = useRef(false)
  const path = `/workspace/support/orders/${encodeURIComponent(orderId)}/conversation`
  useEffect(() => {
    const controller = new AbortController()
    api(path, { headers: { 'X-CSRF-Token': csrf }, signal: controller.signal }).then(value => {
      const parsed = parseConversation(value, orderId); if (!controller.signal.aborted) setState(parsed)
    }).catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    const timer = setTimeout(() => setRevision(v => v + 1), 10000)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [path, orderId, csrf, revision])
  async function submit(original?: { message: string; requestId: string }) {
    if (acting.current) return
    const text = message.trim()
    if (!original && !text) return
    const body = original ?? (pending.current?.message === text ? pending.current : { message: text, requestId: crypto.randomUUID() })
    pending.current = body; acting.current = true; setBusy(true); setError('')
    try {
      const result = parseConversation(await api(path, { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body), timeoutMs: 90000 }), orderId)
      setState(result); setMessage(''); pending.current = null
    } catch (e) { setError(errorMessage(e)) }
    finally { acting.current = false; setBusy(false) }
  }
  if (!state.enabled) return error ? <p role="alert">客服对话暂未连接：{error}。可使用下方售后表单。</p> : null
  return <section className="support-chat" aria-label="订单客服对话">
    <h3>咨询这笔订单</h3><p className="muted">可问支付、物流、售后政策，或说明申请需求。办理前会请你核对并确认。</p>
    {state.turns.map(turn => <article className="support-case" key={turn.requestId}>
      <p><strong>我：</strong>{turn.message}</p>
      <p className="support-answer"><strong>客服：</strong>{turn.result?.answer ?? '正在核对，请稍候或刷新对话。'}</p>
      {!!turn.result?.citations.length && <details><summary>查看回答依据</summary>
        {turn.result.citations.map(citation => <p key={citation.id}>{citation.title ?? '本次读取的服务端记录'}{citation.observedAt ? `（${new Date(citation.observedAt).toLocaleString()}）` : ''}<br />{citation.text ?? '回答中的订单事实取自本次查询；状态可能随后变化，可再次查询最新进度。'}</p>)}
      </details>}
      {turn.result?.preview && <button className="button secondary" disabled={busy} onClick={() => onPreview(turn.result!.preview!)}>核对售后确认卡</button>}
      {turn.result?.actionDraft && <div>
        <p>待确认：{turn.result.actionDraft.label} · 售后编号 {turn.result.actionDraft.caseId}{turn.result.actionDraft.body.trackingNo ? ` · 寄回单号 ${turn.result.actionDraft.body.trackingNo}` : ''}</p>
        <button className="button secondary" disabled={busy} onClick={async () => {
          if (acting.current) return
          acting.current = true; setBusy(true); setError('')
          const draft = turn.result!.actionDraft!, path = `/cases/${draft.caseId}/${actionPaths[draft.action]}`
          try {
            await api('/workspace/support' + path, { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': await supportRequestKey(orderId, path, draft.body) }, body: JSON.stringify(draft.body) })
            setNotice('操作已提交，请刷新售后进度核对最新状态。')
          } catch (e) { setError(errorMessage(e)) } finally { acting.current = false; setBusy(false) }
        }}>确认{turn.result.actionDraft.label}</button>
      </div>}
      {turn.result?.ticketReplyDraft && <div>
        <p>待回复工单 {turn.result.ticketReplyDraft.ticketId}：{turn.result.ticketReplyDraft.body.message}</p>
        <button className="button secondary" disabled={busy} onClick={async () => {
          if (acting.current) return
          acting.current = true; setBusy(true); setError('')
          const draft = turn.result!.ticketReplyDraft!, path = `/tickets/${draft.ticketId}/reply`
          try {
            await api('/workspace/support' + path, { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': await supportRequestKey(orderId, path, draft.body) }, body: JSON.stringify(draft.body) })
            setNotice('补充说明已提交，请刷新工单查看。')
          } catch (e) { setError(errorMessage(e)) } finally { acting.current = false; setBusy(false) }
        }}>确认回复工单</button>
      </div>}
      {turn.result?.ticketDraft && <div>
        <p>工单草稿：{ticketCategories[turn.result.ticketDraft.category]} · {turn.result.ticketDraft.summary}</p>
        <button className="button secondary" disabled={busy} onClick={async () => {
          if (acting.current) return
          acting.current = true; setBusy(true); setError('')
          const body = turn.result!.ticketDraft!
          try {
            await api('/workspace/support/tickets', { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': await supportRequestKey(orderId, '/tickets', body) }, body: JSON.stringify(body) })
            setNotice('工单已登记，可在下方工单列表刷新查看。')
          } catch (e) { setError(errorMessage(e)) } finally { acting.current = false; setBusy(false) }
        }}>确认提交工单</button>
      </div>}
      {turn.status !== 'COMPLETED' && <button className="text-button" disabled={busy} onClick={() => void submit({ message: turn.message, requestId: turn.requestId })}>回查或重试原消息</button>}
    </article>)}
    {error && <p role="alert">{error}。请先刷新对话核对，保留原消息重试。</p>}
    {notice && <p role="status">{notice}</p>}
    <label>向订单客服提问<textarea value={message} maxLength={2000} disabled={busy} onChange={e => setMessage(e.target.value)} /></label>
    <button className="button secondary" disabled={busy || !message.trim()} onClick={() => void submit()}>{busy ? '正在核对…' : '发送客服消息'}</button>
    <button className="text-button" onClick={() => setRevision(v => v + 1)}>刷新客服对话</button>
  </section>
}
