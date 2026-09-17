import { useEffect, useMemo, useRef, useState } from 'react'
import { ExecutionDetail } from './ExecutionDetail'
import { describeEvent, explainObserved, flowDefinition, learningFlow, type LearningEvent } from './lib/learningFlow'
import './ExecutionGraph.css'

export type BackendEvent = LearningEvent
let nextDiagram = 0
const renderer = () => import('mermaid').then(({ default: mermaid }) => {
  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'base', layout: 'dagre',
    fontFamily: 'Microsoft YaHei, sans-serif', themeVariables: { fontSize: '16px', lineColor: '#9baa9f', primaryTextColor: '#243c32' },
    flowchart: { htmlLabels: false, curve: 'basis', nodeSpacing: 20, rankSpacing: 26, padding: 16 }, suppressErrorRendering: true })
  return mermaid
})
let engine: ReturnType<typeof renderer> | undefined

export const renderExecutionDiagram = (id: string, definition: string) =>
  (engine ??= renderer()).then(m => m.render(id, definition))

export function ExecutionGraph({ events }: { events: BackendEvent[] }) {
  const flow = useMemo(() => learningFlow(events), [events])
  const definition = useMemo(() => flowDefinition(flow.steps), [flow])
  const [selected, setSelected] = useState(0), [eventIndex, setEventIndex] = useState<number | null>(null)
  const [error, setError] = useState(false), [ready, setReady] = useState(false)
  const canvas = useRef<HTMLDivElement>(null)
  useEffect(() => { setSelected(0); setEventIndex(null) }, [definition])
  useEffect(() => {
    let cancelled = false
    const host = canvas.current
    if (!host || !flow.steps.length) return
    setReady(false); setError(false)
    const id = `learning-flow-${++nextDiagram}`
    ;(engine ??= renderer()).then(m => m.render(id, definition)).then(({ svg }) => {
      if (cancelled) return
      // Source is generated from fixed labels; Mermaid strict mode sanitizes its SVG.
      host.innerHTML = svg
      host.querySelector('svg')?.setAttribute('aria-label', '本次操作的学习流程图')
      flow.steps.forEach((step, i) => {
        const node = host.querySelector<SVGGElement>(`g.node[id^="${id}-flowchart-${step.id}-"]`)
        if (!node) return
        node.setAttribute('role', 'button'); node.setAttribute('tabindex', '0')
        node.setAttribute('aria-label', step.title); node.dataset.step = String(i)
        node.setAttribute('aria-pressed', String(i === 0))
        const choose = () => { setSelected(i); setEventIndex(null) }
        node.addEventListener('click', choose)
        node.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose() } })
      })
      setReady(true)
    }).catch(() => { if (!cancelled) setError(true) })
    return () => { cancelled = true; host.replaceChildren() }
  }, [definition, flow])
  useEffect(() => {
    canvas.current?.querySelectorAll<SVGGElement>('g.node[data-step]').forEach(node => {
      const active = Number(node.dataset.step) === selected
      node.setAttribute('aria-pressed', String(active))
      node.querySelectorAll<SVGElement>(':scope > rect, :scope > polygon, :scope > path').forEach(shape => {
        if (active) { shape.style.setProperty('fill', '#fff0e5', 'important'); shape.style.setProperty('stroke', '#ec6c25', 'important') }
        else { shape.style.removeProperty('fill'); shape.style.removeProperty('stroke') }
      })
    })
  }, [selected, ready])
  const step = flow.steps[selected] ?? flow.steps[0]
  const initialEvent = step?.events.find(e => e.input && typeof e.input === 'object' && 'request' in e.input) ?? step?.events[0]
  const current = eventIndex == null ? initialEvent : step?.events[eventIndex] ?? initialEvent
  const copy = current && describeEvent(current)
  return <div className="execution-learning">
    {!step ? <p>这次没有记录到具体业务步骤。</p> : <>
      <div className="learning-intro"><h4>{flow.cart ? '这一次，下单怎样经过后台？' : '这次操作，后台做了什么？'}</h4>
        <p>按业务阶段归纳，箭头表示阅读顺序。只显示本次有记录的阶段；点节点看具体解释。</p></div>
      <div className="learning-layout">
        <div className="learning-map">
          <div ref={canvas} className="learning-mermaid" />
          {!ready && !error && <p role="status">正在绘制流程图…</p>}
          {error && <div><p>流程图暂不可用，仍可选择步骤查看：</p>{flow.steps.map((s, i) => <button key={s.id} onClick={() => { setSelected(i); setEventIndex(null) }}>{s.title}</button>)}</div>}
          <p className="learning-legend">红色：包含异常或回滚　黄色：结果待确认</p>
        </div>
        <section className="learning-detail" aria-label="后台步骤说明">
          <span className="learning-step-number">第 {selected + 1} 步</span><h4>{step.title}</h4><p>{step.why}</p>
          {current && copy && <>
            {step.events.length > 1 && <div className="learning-actions" aria-label="本阶段的具体操作">{step.events.map((event, i) => <button type="button" key={i}
              aria-pressed={event === current} onClick={() => setEventIndex(i)}>{describeEvent(event).title}{step.events.filter(e => describeEvent(e).title === describeEvent(event).title).length > 1 ? `（记录 ${i + 1}）` : ''}</button>)}</div>}
            {copy.description !== step.why && <><h5>{copy.title}</h5><p>{copy.description}</p></>}
            <div className="learning-narrative"><h5>本次记录能说明什么</h5>{explainObserved(current).map((fact, i) => <p key={i}>{fact}</p>)}</div>
            <details className="learning-code" key={current.id ?? `${selected}-${eventIndex}`}><summary>查看这一步的代码和参数</summary><ExecutionDetail event={current} symbol={current.name} /></details>
          </>}
        </section>
      </div>
    </>}
  </div>
}
