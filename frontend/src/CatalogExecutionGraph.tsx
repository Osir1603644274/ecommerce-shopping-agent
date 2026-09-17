import { useEffect, useMemo, useRef, useState } from 'react'
import type { FlowNode } from './lib/workspace'
import { catalogFlowDefinition } from './lib/catalogFlow'
import { renderExecutionDiagram } from './ExecutionGraph'
import './CatalogExecutionGraph.css'

let serial = 0
export function CatalogExecutionGraph({ nodes }: { nodes: FlowNode[] }) {
  const definition = useMemo(() => catalogFlowDefinition(nodes), [nodes])
  const host = useRef<HTMLDivElement>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let cancelled = false
    setError(false)
    renderExecutionDiagram(`catalog-execution-${++serial}`, definition).then(({ svg }) => {
      if (!cancelled && host.current) {
        host.current.innerHTML = svg
        host.current.querySelector('svg')?.setAttribute('aria-label', '普通商品实际执行架构图')
      }
    }).catch(() => { if (!cancelled) setError(true) })
    return () => { cancelled = true }
  }, [definition])
  return <section className="catalog-architecture" aria-label="普通商品执行架构">
    <h4>这轮怎样从需求到答案？</h4>
    <p>当前是固定编排：模型选择操作，程序依次执行。尚无工具返回后再由模型自由规划的 ReAct 循环。</p>
    <div ref={host} className="catalog-architecture-diagram" />
    {error && <p>图形暂不可用，下面的步骤仍可点击查看。</p>}
    <p className="catalog-architecture-legend">绿色：已有完成记录 · 虚线框：尚无完成记录。点击下方步骤查看本次输入、条件变化与结果。</p>
  </section>
}
