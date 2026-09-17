import { api, ApiError } from './api'
import { isProductId } from './identity'
import type { ProductId } from './identity'
import type { ExecutionMetadata } from '../ExecutionDetail'

export interface ProductCard {
  id: ProductId
  title: string
  brand: string
  category?: string
  priceMinor: number | null
  currency: string
  priceKind?: string | null
  available?: number | null
  purchasable?: boolean
  evidence?: Evidence[]
  evidenceNotice?: string
}
export interface Evidence {
  id: string; model: string; text: string; field?: string; url: string
  region?: string; conditions?: unknown; software?: string; section?: string
}
export interface FlowNode extends ExecutionMetadata {
  kind?: 'tool'
  label: string; outcome?: string; durationMs?: number; detail?: Record<string, unknown>
  code?: string; error?: string
}
export interface Run {
  id: string; revision: number; mode: 'continuous' | 'step'; status: string
  nodes: FlowNode[]; nextStage?: string; notice?: string; requestId: string; canResume?: boolean
}
export interface Message {
  source?: string
  role: 'user' | 'assistant'
  content: string
  requestId: string
  cards?: ProductCard[]
  flow?: FlowNode[]
}
export interface Receipt {
  id?: string
  orderId?: string
  orderNo?: string
  status?: string
  payableMinor?: number
}
export interface Checkout {
  proposal: {
    confirmationId: string
    action: 'create_order' | 'create_payment' | 'cancel_order' | 'create_refund'
    expiresAt: string
    preview: {
      title?: string
      quantity?: number
      unitPriceMinor?: number
      payableMinor?: number
      currency?: string
      refundItems?: { itemId: ProductId; title: string; quantity: number; amountMinor: number }[]
      reason?: string
      order?: Receipt & { items?: { titleSnapshot: string; quantity: number }[] }
    }
  }
  pending: boolean
  outcome: { status?: string; message?: string; result?: Receipt } | null
}
export interface Workspace {
  recommendationDemo?: { caseId: string; revision: number } | null
  answerStream?: { runId: string; requestId: string; text: string; sequence: number; status: string } | null
  conversationId?: string
  messages: Message[]
  cards: ProductCard[]
  selection: { product: ProductCard; quantity: number } | null
  checkout: Checkout | null
  csrfToken?: string | null
  run?: Run | null
}

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string'
const integer = (value: unknown): value is number => Number.isSafeInteger(value)
const amount = (value: unknown) => integer(value) && value >= 0
const optional = (value: unknown, check: (value: unknown) => boolean) => value == null || check(value)
const sourceUrl = (value: unknown): boolean => {
  if (!text(value)) return false
  try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password } catch { return false }
}
const evidence = (value: unknown): boolean => record(value) &&
  ['id', 'model', 'text'].every(k => text(value[k])) && sourceUrl(value.url) &&
  ['field', 'region', 'software', 'section'].every(k => optional(value[k], text))
const metadata = (n: Record<string, unknown>) =>
  ['id', 'parentId', 'startedAt', 'finishedAt'].every(k => optional(n[k], text)) && optional(n.cause, record) &&
  optional(n.source, s => record(s) && ['file', 'symbol', 'function', 'sha256', 'snippet', 'notice', 'scope'].every(k => optional(s[k], text)) &&
    optional(s.line, v => integer(v) && v > 0))
const flow = (value: unknown): boolean => Array.isArray(value) && value.every(n => record(n) &&
  metadata(n) &&
  text(n.label) && optional(n.kind, k => k === 'tool') && optional(n.outcome, text) &&
  optional(n.durationMs, d => typeof d === 'number' && Number.isFinite(d) && d >= 0) &&
  optional(n.detail, record) && optional(n.code, text) && optional(n.error, text))
const run = (value: unknown): boolean => record(value) && text(value.id) && text(value.requestId) &&
  integer(value.revision) && value.revision > 0 && ['continuous', 'step'].includes(String(value.mode)) &&
  ['running', 'pausing', 'paused', 'waiting', 'interrupted', 'completed', 'ended', 'failed', 'clarification'].includes(String(value.status)) &&
  flow(value.nodes) && optional(value.nextStage, text) && optional(value.notice, text) && optional(value.canResume, v => typeof v === 'boolean')
const product = (value: unknown): boolean =>
  record(value) && isProductId(value.id) && text(value.title) && text(value.brand) &&
  text(value.currency) && optional(value.priceMinor, amount) && optional(value.available, amount) &&
  optional(value.priceKind, text) && optional(value.category, text) &&
  optional(value.purchasable, value => typeof value === 'boolean') &&
  optional(value.evidence, rows => Array.isArray(rows) && rows.every(evidence)) && optional(value.evidenceNotice, text)
const receipt = (value: unknown): boolean => record(value) &&
  text(value.id) && value.id.trim().length > 0 &&
  ['id', 'orderId', 'orderNo', 'status'].every(key => optional(value[key], text)) &&
  optional(value.payableMinor, amount)
const checkout = (value: unknown): boolean => {
  if (!record(value) || typeof value.pending !== 'boolean' || !record(value.proposal)) return false
  const p = value.proposal
  if (!text(p.confirmationId) || !/^cfm-[a-zA-Z0-9_-]{10,64}$/.test(p.confirmationId) ||
      !['create_order', 'create_payment', 'cancel_order', 'create_refund'].includes(String(p.action)) ||
      !text(p.expiresAt) || !Number.isFinite(Date.parse(p.expiresAt)) || !record(p.preview)) return false
  const preview = p.preview
  if (!optional(preview.reason, text) || !optional(preview.refundItems, items => Array.isArray(items) &&
      items.every(i => record(i) && isProductId(i.itemId) && text(i.title) && integer(i.quantity) && i.quantity > 0 && amount(i.amountMinor)))) return false
  if (!optional(preview.title, text) || !optional(preview.currency, text) ||
      !optional(preview.quantity, v => integer(v) && v >= 1 && v <= 20) ||
      !optional(preview.unitPriceMinor, amount) || !optional(preview.payableMinor, amount)) return false
  if (preview.order != null) {
    if (!receipt(preview.order) || !record(preview.order) || !optional(preview.order.items, items =>
      Array.isArray(items) && items.every(item => record(item) && text(item.titleSnapshot) &&
        integer(item.quantity) && item.quantity > 0))) return false
  }
  // A missing amount must not be displayed as a successful zero-cost purchase.
  if (p.action === 'create_order' ? !amount(preview.payableMinor) :
    !amount(preview.payableMinor) && !(record(preview.order) && amount(preview.order.payableMinor))) return false
  return value.outcome == null || (record(value.outcome) && optional(value.outcome.status, text) &&
    optional(value.outcome.message, text) && optional(value.outcome.result, receipt))
}

export function parseWorkspace(value: unknown): Workspace {
  if (
    !record(value) ||
    !('selection' in value) || !('checkout' in value) ||
    !Array.isArray(value.messages) ||
    !value.messages.every(m => record(m) && ['user', 'assistant'].includes(String(m.role)) &&
      text(m.content) && text(m.requestId) && optional(m.cards, c => Array.isArray(c) && c.every(product)) && optional(m.flow, flow)) ||
    !Array.isArray(value.cards) || !value.cards.every(product) ||
    !optional(value.selection, s => record(s) && product(s.product) && integer(s.quantity) && s.quantity >= 1 && s.quantity <= 20) ||
    !optional(value.checkout, checkout) || !optional(value.csrfToken, text) || !optional(value.run, run) ||
    !optional(value.recommendationDemo, v => record(v) && text(v.caseId) && integer(v.revision) && v.revision >= 0) ||
    !optional(value.conversationId, v => text(v) && /^[a-f0-9]{32}$/.test(v)) ||
    !optional(value.answerStream, v => record(v) && text(v.runId) && text(v.requestId) && text(v.text) && v.text.length <= 12000 &&
      integer(v.sequence) && v.sequence >= 0 && ['generating', 'paused', 'interrupted', 'completed'].includes(String(v.status)))
  )
    throw new Error('购物空间数据格式不正确')
  return value as unknown as Workspace
}
let loading: { token: string; promise: Promise<Workspace> } | null = null
export function loadWorkspace(token: string) {
  if (!loading || loading.token !== token) {
    const promise = api('/workspace', { headers: { 'X-CSRF-Token': token } }).then(parseWorkspace)
    loading = { token, promise }
    void promise
      .finally(() => {
        if (loading?.promise === promise) loading = null
      })
      .catch(() => {})
  }
  return loading.promise
}

export async function streamChat(
  message: string,
  requestId: string,
  csrf: string,
  signal: AbortSignal,
  onText: (text: string) => void,
): Promise<Workspace> {
  const response = await fetch('/api/commerce-demo/workspace/chat', {
    method: 'POST',
    credentials: 'same-origin',
    signal,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify({ message, requestId }),
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new ApiError(typeof body?.detail === 'string' ? body.detail : '对话服务暂时不可用',
      response.status, null, typeof body?.detail === 'string' ? body.detail : null)
  }
  const reader = response.body.getReader(),
    decoder = new TextDecoder()
  let buffer = '',
    text = '',
    completed: Workspace | null = null
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        for (const line of block.split('\n')) {
          if (!line.startsWith('data: ')) continue
          const event = JSON.parse(line.slice(6))
          if (event.type === 'delta') {
            text += event.text
            onText(text)
          }
          if (event.type === 'error') throw new Error(event.message)
          if (event.type === 'complete') completed = parseWorkspace(event.workspace)
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
  if (!completed) throw new Error('连接中断，回答尚未完成；可恢复对话后重试')
  return completed
}
