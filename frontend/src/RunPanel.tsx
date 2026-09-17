import { useEffect, useRef, useState } from 'react'
import type { FlowNode, Run } from './lib/workspace'
import { ExecutionDetail } from './ExecutionDetail'
import { outcomeExplanation } from './lib/explainExecution'
import { isCatalogFlow } from './lib/catalogFlow'
import { CatalogExecutionGraph } from './CatalogExecutionGraph'

const labels: Record<string, string> = {
  pre_harness: '准备本轮需求', react_decision: '选择下一步操作',
  task_manager: '理解需求', task_state: '更新任务', context_pack: '准备上下文',
  planner: '规划', react_policy: '决策', executor: '执行工具', validator: '校验证据',
  replanner: '调整计划', final_answer: '生成回答', answer_buffer: '生成回答',
  search_products: '检索商品', compare_products: '候选核验与资料汇总', done: '完成',
  TaskManager: '理解需求', '更新 TaskState': '更新任务', '构建 ContextPack': '准备上下文',
  'Planner 规划': '规划', 'Executor 执行一步': '执行工具', 'Validator 校验': '校验证据', '生成最终回答': '生成回答',
}
const fieldLabels: Record<string, string> = { taskRevision: '任务版本', tool: '实际工具', toolOk: '工具成功',
  failureCode: '失败原因代码', failedBatchCount: '失败批次', failureReasons: '批次失败原因',
  count: '召回数量', query: '查询内容', userQuery: '用户需求', productIds: '候选商品编号', code: '结果代码',
  purpose: '工具职责', evidencePolicy: '调用规则', evidenceCount: '资料条数', status: '结果状态',
  transport: '调用协议', version: '知识库版本', plannedTools: '计划调用的工具', decision: '校验决定' }
const technicalFields = new Set(['workflow','phase','scopeId','executionMode','modelDurationMs','action'])
const outcomes: Record<string, string> = { passed: '通过', failed: '失败', planned: '已生成计划',
  step_executed: '步骤已执行', generated: '回答已生成', unavailable: '未获得结果',
  insufficient_evidence: '证据不足', completed: '已完成', tool_succeeded: '工具成功', tool_failed: '工具失败' }
const duties: Record<string, string> = {
  planner: '根据当前需求和可用工具生成执行计划。', executor: '执行计划中的工具；下方明细列出本轮实际调用。',
  validator: '检查工具结果是否满足约束，决定能否用于回答。', final_answer: '汇总已通过校验的结果，生成用户可见回答。',
  compare_products: '在候选内核验需求、汇集型号资料；不是凭商品标题给续航排名。',
  search_products: '从商品目录召回候选商品，检索命中不等于实测结论。' }
export const isActive = (run?: Run | null) => !!run && !['completed', 'ended'].includes(run.status)

export function Flow({ nodes }: { nodes: FlowNode[] }) {
  const [selected, setSelected] = useState<number | null>(null)
  const detailRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (selected != null) detailRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])
  if (!nodes.length) return <p className="flow-empty">尚无已完成步骤；不会预先填充调用结果。</p>
  const current = selected == null ? null : nodes[selected]
  const entries = Object.entries(current?.detail || {}).filter(([, v]) => v != null)
  const stages = nodes.map((n, i) => ({ n, i })).filter(({ n }) => n.kind !== 'tool')
  const tools = nodes.map((n, i) => ({ n, i })).filter(({ n }) => n.kind === 'tool')
  const nodeButton = ({ n, i }: { n: FlowNode; i: number }, ordinal: number) => <button type="button" key={i}
    className={`flow-node ${n.outcome === 'failed' ? 'failed' : ''}`} aria-pressed={selected === i}
    onClick={() => setSelected(selected === i ? null : i)}>
    <span>{ordinal + 1}</span>{labels[n.label] || n.label}
    <small>{outcomeExplanation(n.outcome || '')}</small>
  </button>
  return <div className="execution-flow">
    {isCatalogFlow(nodes) && <CatalogExecutionGraph nodes={nodes} />}
    <div className="flow-track" aria-label="实际执行流程">
      {stages.map(nodeButton)}
    </div>
    {!!tools.length && <><p className="flow-empty">工具调用明细（发生在执行阶段，非回答后的额外步骤）</p>
      <div className="flow-track tool-track" aria-label="工具调用明细">{tools.map(nodeButton)}</div></>}
    {current && <section ref={detailRef} className="flow-detail" aria-label="节点详情">
      <strong>{labels[current.label] || current.label}</strong>
      {duties[current.label] && <p>职责：{duties[current.label]}</p>}
      <dl>
        <div><dt>执行状态</dt><dd>{outcomes[current.outcome || ''] || current.outcome || '未记录'}</dd></div>
        {current.kind === 'tool' && <div><dt>实际接口</dt><dd>{current.label}</dd></div>}
        {entries.filter(([k]) => !['taskRevision','toolDurationMs'].includes(k) && !technicalFields.has(k)).map(([k, v]) => <div key={k}><dt>{fieldLabels[k] || ({ model: '理解模型', retrievalExecuted: '本步是否新检索', modelCalled: '本步是否调用回答模型' } as Record<string,string>)[k] || k}</dt><dd>{typeof v === 'object' ? JSON.stringify(v) : typeof v === 'boolean' ? (v ? '是' : '否') : String(v)}</dd></div>)}
      </dl>
      {current.error && <p>{current.error}</p>}
      {(current.code || entries.some(([k]) => technicalFields.has(k))) && <details className="flow-technical"><summary>技术详情</summary>
        {current.code && <code>{current.code}</code>}<pre>{JSON.stringify(Object.fromEntries(entries.filter(([k]) => technicalFields.has(k))), null, 2)}</pre>
      </details>}
      <ExecutionDetail event={current} dataFirst={current.detail?.workflow === 'catalog_workspace_v1'} />
      {!entries.length && current.input == null && current.output == null && <p>此记录未保留额外输入输出摘要；不会补造历史细节。请查看工具调用明细。</p>}
    </section>}
  </div>
}

export function RunPanel({ run, busy, onCommand, composerControls = false }: {
  run: Run; busy: boolean; onCommand: (op: string, answer?: string) => void; composerControls?: boolean
}) {
  const [answer, setAnswer] = useState('')
  const status: Record<string, string> = { running: '正在执行', pausing: '正在暂停', paused: '已暂停',
    waiting: '等待下一步', interrupted: '等待恢复', completed: '已完成', ended: '已结束', failed: '安全停止', clarification: '需要补充信息' }
  return <section className="run-panel" data-status={run.status} aria-label="本轮执行控制">
    <div className="run-heading"><strong>{status[run.status] || run.status}</strong><small>{run.mode === 'step' ? '单步调试' : '连续执行'}</small></div>
    {run.notice && !(composerControls && run.status === 'clarification') && <p role="status">{run.notice}</p>}
    {run.nextStage && run.nextStage !== 'done' && <p>下一步：{labels[run.nextStage] || run.nextStage}</p>}
    <details open={run.mode === 'step'}><summary>实际执行过程 · {run.nodes.length} 条记录</summary><Flow nodes={run.nodes} /></details>
    {!composerControls && run.status === 'clarification' && <textarea aria-label="回答澄清问题" value={answer} onChange={e => setAnswer(e.target.value)} />}
    <div className="run-actions">
      {!composerControls && run.mode === 'continuous' && ['running', 'pausing'].includes(run.status) &&
        <button type="button" disabled={busy || run.status === 'pausing'} onClick={() => onCommand('pause')}>{run.status === 'pausing' ? '正在暂停…' : '暂停'}</button>}
      {run.mode === 'step' && ['waiting', 'interrupted'].includes(run.status) &&
        <button type="button" disabled={busy} onClick={() => onCommand('step')}>下一步</button>}
      {run.mode === 'continuous' && run.canResume === true && ['paused', 'interrupted', 'clarification', 'failed'].includes(run.status) && !(composerControls && run.status === 'clarification') &&
        <button type="button" disabled={busy || (run.status === 'clarification' && !answer.trim())} onClick={() => onCommand('continue', answer)}>{run.status === 'clarification' ? '提交并继续' : '从原检查点继续'}</button>}
      {run.mode === 'continuous' && ['failed','interrupted'].includes(run.status) && run.canResume === false &&
        <p>本轮没有可恢复的检查点。可以直接发送新问题，或点击“新对话”。</p>}
      {(!composerControls || run.mode === 'step') && isActive(run) && !['running', 'pausing'].includes(run.status) &&
        <button type="button" className="secondary" disabled={busy} onClick={() => onCommand('end')}>结束本轮，修改需求</button>}
    </div>
  </section>
}
