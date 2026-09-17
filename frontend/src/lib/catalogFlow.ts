import type { FlowNode } from './workspace'

const phases = ['prepare', 'retrieve', 'answer', 'publish'] as const
export const isCatalogFlow = (nodes: FlowNode[]) => nodes.some(n => n.detail?.workflow === 'catalog_workspace_v1')

/** Fixed labels only. Query text, titles and model output never enter Mermaid. */
export function catalogFlowDefinition(nodes: FlowNode[]) {
  const node = (phase: string) => nodes.find(n => n.detail?.phase === phase)
  const action = node('prepare')?.detail?.action
  const searching = ['search', 'refine', 'new'].includes(String(action))
  const reuse = ['compare', 'undo'].includes(String(action))
  const lines = ['flowchart TB',
    'classDef pending fill:#f6f6f3,stroke:#b9c4bb,stroke-dasharray:5 4,color:#6b786e;',
    'classDef done fill:#eef5e9,stroke:#6c916f,color:#243c32;',
    'U["用户输入"]', 'P{"理解需求与选择操作"}', 'U --> P',
    `A["${action === 'cancel' ? '返回取消结果' : action === 'clarify' ? '返回澄清问题' : action === 'compare' ? '基于当前候选比较' : '审核商品与条件并生成回答'}"]`,
    'S["保存并展示"]']
  if (searching) lines.push('R["调用两个商品来源检索"]', 'O["取得候选与原文证据"]', 'P -->|需要搜索| R', 'R --> O', 'O --> A')
  else if (reuse) lines.push('R["读取已保存候选"]', 'O["当前商品与原文证据"]', 'P -->|复用候选| R', 'R --> O', 'O --> A')
  else if (action === 'cancel' || action === 'clarify') lines.push('P -->|取消或澄清，无新检索| A')
  else lines.push('R["检索或复用候选"]', 'P -->|旧记录未区分分支| R', 'R --> A')
  lines.push('A --> S')
  const ids: Record<string,string[]> = {prepare:['U','P'],retrieve:['R','O'],answer:['A'],publish:['S']}
  for (const phase of phases) {
    const known = node(phase)
    for (const id of ids[phase]) {
      if (id === 'O' && !searching && !reuse) continue
      if (id === 'R' && (action === 'cancel' || action === 'clarify')) continue
      lines.push(`class ${id} ${known?.outcome === 'completed' ? 'done' : 'pending'};`)
    }
  }
  return lines.join('\n')
}
