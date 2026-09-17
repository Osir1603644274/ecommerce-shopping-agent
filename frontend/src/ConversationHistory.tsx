import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './lib/api'
import { MessageBody } from './MessageBody'
import { Flow } from './RunPanel'
import type { Message } from './lib/workspace'

interface Entry { id: string; title: string; updated_at: string }
export function ConversationHistory({ csrf, onClose, onContinue }: { csrf: string; onClose: () => void; onContinue?: (chat: { id: string; title: string; messages: Message[] }) => void }) {
  const [rows, setRows] = useState<Entry[]>([])
  const [next, setNext] = useState<number | null>(null)
  const [selected, setSelected] = useState<{ id: string; title: string; messages: Message[] } | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const generation = useRef(0)
  useEffect(() => {
    dialog.current?.showModal()
    return () => { generation.current++; dialog.current?.close() }
  }, [])
  useEffect(() => {
    let live = true
    void api('/workspace/conversations', { headers: { 'X-CSRF-Token': csrf } }).then(data => {
      if (!live) return
      const result = data as { conversations: Entry[]; nextOffset: number | null }
      setRows(result.conversations); setNext(result.nextOffset)
    }).catch(e => { if (live) setError(errorMessage(e)) })
    return () => { live = false }
  }, [csrf])
  async function more() {
    if (busy || next == null) return
    setBusy(true)
    try {
      const data = await api(`/workspace/conversations?offset=${next}`, { headers: { 'X-CSRF-Token': csrf } }) as { conversations: Entry[]; nextOffset: number | null }
      setRows(old => [...old, ...data.conversations]); setNext(data.nextOffset)
    } catch (e) { setError(errorMessage(e)) } finally { setBusy(false) }
  }
  async function open(id: string) {
    const seq = ++generation.current
    setError(''); setBusy(true)
    try {
      const data = await api(`/workspace/conversations/${encodeURIComponent(id)}`, { headers: { 'X-CSRF-Token': csrf } }) as { id: string; title: string; messages: Message[] }
      if (seq === generation.current) setSelected(data)
    } catch (e) { if (seq === generation.current) setError(errorMessage(e)) }
    finally { if (seq === generation.current) setBusy(false) }
  }
  return <dialog className="conversation-dialog" ref={dialog} onCancel={onClose} aria-label="历史对话">
    <header><h2>历史对话</h2><button onClick={onClose}>返回当前对话</button></header>
    <p>这里查看已保存的记录；继续对话不会重发旧交易，购买时重新核验价格与库存。</p>
    {error && <p role="alert">{error}</p>}
    <div className="conversation-layout"><nav aria-label="历史会话列表">
      {rows.map(row => <button key={row.id} aria-pressed={selected?.id === row.id} onClick={() => void open(row.id)}>
        {row.title}<small>{new Date(row.updated_at).toLocaleString('zh-CN')}</small>
      </button>)}
      {!rows.length && <p>尚无历史对话</p>}
      {next != null && <button disabled={busy} onClick={() => void more()}>加载更多</button>}
    </nav><section aria-label="历史消息" aria-busy={busy}>
      {!selected && <p>请选择一个会话</p>}
      {selected && <h3>{selected.title}</h3>}
      {selected && onContinue && <button disabled={busy} onClick={() => onContinue(selected)}>继续这段对话</button>}
      {selected?.messages.map(m => <article key={`${m.role}:${m.requestId}`} className={`history-message ${m.role}`}>
        <strong>{m.role === 'user' ? '你' : '拾物 AI'}</strong><MessageBody text={m.content} />
        {!!m.cards?.length && <p>本轮商品：{m.cards.map(c => c.title).join('；')}</p>}
        {!!m.flow?.length && <details><summary>查看本轮执行记录</summary><Flow nodes={m.flow} /></details>}
      </article>)}
    </section></div>
  </dialog>
}
