import { useEffect, useState, useSyncExternalStore } from 'react'
import { api, errorMessage } from './lib/api'
import { configureObserver, observerSnapshot, subscribeObserver } from './lib/backendObserver'
import './BackendObserver.css'
import { ExecutionGraph } from './ExecutionGraph'
import { requestExplanation } from './lib/explainExecution'
import type { ExecutionMetadata } from './ExecutionDetail'
import { requestJourney } from './lib/requestJourney'

type Event = ExecutionMetadata & { kind: string; name: string; outcome: string; durationMs: number; affectedRows: number | null }
type Call = ExecutionMetadata & { method: string; path: string; status: number; traceId: string | null;
  detail: { events: Event[]; droppedEvents: number } | null }
type Trace = { calls: Call[]; scope: string }

export function BackendObserver({ csrf, loggedIn }: { csrf: string; loggedIn: boolean }) {
  const [enabled, setEnabled] = useState(false), [selected, setSelected] = useState<number | null>(null)
  const [data, setData] = useState<Trace | null>(null), [error, setError] = useState('')
  const [callIndex, setCallIndex] = useState(0)
  const requests = useSyncExternalStore(subscribeObserver, observerSnapshot)
  useEffect(() => { configureObserver(enabled && loggedIn); setSelected(null); setData(null); setError('')
    return () => configureObserver(false)
  }, [enabled, loggedIn, csrf])
  const ticket = requests.find(r => r.id === selected)?.ticket
  useEffect(() => {
    setData(null); setError(''); setCallIndex(0)
    if (!ticket) return
    const controller = new AbortController()
    api('/backend-traces/' + encodeURIComponent(ticket), { signal: controller.signal, headers: { 'X-CSRF-Token': csrf } })
      .then(value => {
        if (controller.signal.aborted) return
        const trace = value as Trace | null
        if (!trace || !Array.isArray(trace.calls) || trace.calls.some(call => !call || typeof call.path !== 'string'
          || typeof call.method !== 'string' || (call.detail != null && !Array.isArray(call.detail.events))))
          throw new Error('后端记录格式不完整，无法绘制可靠的流程。')
        setData(trace)
      })
      .catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)) })
    return () => controller.abort()
  }, [ticket, csrf])
  return <section id="backend-observer" className="backend-observer" aria-label="Java 后端执行详情">
    <label><input type="checkbox" checked={enabled} disabled={!loggedIn} onChange={e => setEnabled(e.target.checked)} /> 记录 Java 后端执行详情</label>
    {!loggedIn && <small>登录后开启，只查看自己当前会话的请求。</small>}
    {enabled && loggedIn && <>
      <p>选一次操作，看看它在后台做了什么。</p>
      {!requests.length && <p>现在点击订单、收藏或优惠与抢购中的操作，记录会出现在这里。</p>}
      <div className="backend-request-list">{requests.map(r => <button key={r.id} className="button secondary"
        aria-pressed={selected === r.id} onClick={() => setSelected(r.id)}>
        {r.cause?.label ?? requestExplanation(r.path, r.method)}
        <small>{r.cause?.trigger === 'user' ? '你的操作' : r.cause ? '页面自动请求' : '页面请求'} · {r.status === 0 ? '未收到回复' : r.status < 400 ? '已收到回复' : '请求未完成'}</small>
      </button>)}</div>
      {selected != null && requests.find(r => r.id === selected) && (() => {
        const r = requests.find(r => r.id === selected)!
        return <details className="backend-technical"><summary>查看页面请求</summary>
          <p>这个接口由上方操作触发，用于把需求交给服务端。</p>
          <code>{r.method} {r.path}</code><p>返回状态：{r.status || '未收到回复'}</p>
          {r.cause?.code && <code>{r.cause.code}</code>}
        </details>
      })()}
      {selected && !ticket && <p>未取得后端追踪回执：服务可能未启用调试、尚未登录，或请求没有返回。不能据此认定 Java 没有执行。</p>}
      {error && <p role="alert">记录暂不可用或已过期：{error}</p>}
      {ticket && !data && !error && <p role="status">读取本次后端记录…</p>}
      {data && <>
        <details className="backend-technical"><summary>记录范围</summary><p>{data.scope}</p></details>
        {!data.calls.length && <p>本次没有捕获到已接入的同步 Java 调用；这不代表整个后台没有动作。</p>}
        {data.calls.length > 1 && <nav className="backend-journey" aria-label="这次点击涉及的后台环节">
          <h3>一次操作，分几个环节完成</h3>
          <p>{requestJourney(data.calls, requests.find(r => r.id === selected)?.path ?? '').explanation}</p>
          <ol>{requestJourney(data.calls, requests.find(r => r.id === selected)?.path ?? '').notes.map((note, i) => <li key={i}>
            <button type="button" aria-pressed={callIndex === i} onClick={() => setCallIndex(i)}><span>{i + 1}</span>{note.title}
              <small>{data.calls[i].status === 0 ? '回复未确认' : data.calls[i].status >= 400 ? '请求未完成' : '已收到回复'}</small></button>
          </li>)}</ol><p>编号是调用记录的展示顺序，不表示每个环节一定串行执行。</p>
        </nav>}
        {data.calls.slice(callIndex, callIndex + 1).map(call => <article key={`${selected}-${callIndex}`} className="backend-call">
          <h3>{requestJourney(data.calls, requests.find(r => r.id === selected)?.path ?? '').notes[callIndex].title}</h3>
          <p>{requestJourney(data.calls, requests.find(r => r.id === selected)?.path ?? '').notes[callIndex].why}</p>
          <details className="backend-technical"><summary>接口与传入数据</summary>
            <p>Agent 为这次页面请求调用了下面的 Java 接口。</p><code>{call.method} {call.path}</code>
            <p>返回状态：{call.status || '未收到回复'}</p><pre>{JSON.stringify(call.input ?? {}, null, 2)}</pre>
          </details>
          {!call.detail ? <p>Java 未提供详情或记录已过期。</p> : <>
            {call.detail.droppedEvents > 0 && <p>记录已截断，图中只展示已保存的部分。</p>}
            <ExecutionGraph key={call.traceId ?? callIndex} events={call.detail.events} />
          </>}
        </article>)}
      </>}
    </>}
  </section>
}
