import { isProductId } from './identity'
import type { ProductId } from './identity'

export const supportTypes = { REFUND_ONLY: '仅退款', RETURN_REFUND: '退货退款', EXCHANGE: '同款同规格换货' } as const
export type SupportType = keyof typeof supportTypes
export const supportPhases: Record<string, string> = {
  AWAITING_REVIEW: '等待审核', AWAITING_RETURN: '请登记寄回单号', RETURN_IN_TRANSIT: '等待仓库收货',
  AWAITING_INSPECTION: '等待验收', REFUND_PENDING: '退款处理中，尚未到账', WAITING_STOCK: '正在核对换货库存',
  WAITING_CHOICE: '换货待选择：等待或转退款', REPLACEMENT_READY: '已预占库存，等待补发',
  REPLACEMENT_RELEASING: '预占超时，正在核实库存释放',
  REPLACEMENT_SHIPPED: '补发已出库，等待签收', COMPLETED: '办理完成', REJECTED: '申请未通过',
  CANCELLED: '申请已撤销', NEEDS_REVIEW: '需人工核实，请提交关联工单',
}
export interface SupportCase {
  id: string; orderId: string; itemId: ProductId; quantity: number; type: SupportType; phase: string;
  amountMinor: number; currency: string; specification: string | null; version: number
}
export interface SupportPreview { previewId: string; amountMinor: number; currency: string; expiresAt: string; specification?: string | null }
export function parseSupportCase(value: unknown): SupportCase {
  const v = value as SupportCase
  if (!v || typeof v.id !== 'string' || typeof v.orderId !== 'string' || !isProductId(v.itemId)
    || !Number.isSafeInteger(v.quantity) || v.quantity < 1 || !Object.hasOwn(supportTypes, v.type)
    || !Object.hasOwn(supportPhases, v.phase) || !Number.isSafeInteger(v.amountMinor) || v.amountMinor < 0
    || typeof v.currency !== 'string' || !Number.isSafeInteger(v.version) || v.version < 0
    || !(v.specification === null || typeof v.specification === 'string'))
    throw new Error('售后数据不完整，请刷新核对')
  return v
}
export function parseSupportPreview(value: unknown): SupportPreview {
  const v = value as SupportPreview
  if (!v || typeof v.previewId !== 'string' || !Number.isSafeInteger(v.amountMinor) || v.amountMinor < 0
    || typeof v.currency !== 'string' || typeof v.expiresAt !== 'string' || !Number.isFinite(Date.parse(v.expiresAt))
    || !(v.specification == null || typeof v.specification === 'string'))
    throw new Error('确认卡数据不完整，请重新预览')
  return v
}
export function specificationLabel(value: string | null): string {
  if (!value) return '规格需核实'
  try { const v = JSON.parse(value); return String(v.label || v.code || '规格需核实') } catch { return '规格需核实' }
}
const fallbackKeys = new Map<string, string>()
export async function supportRequestKey(scope: string, path: string, body: object): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(body)))
  const slot = `support-request:${scope}:${path}:${Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('')}`
  try {
    const existing = sessionStorage.getItem(slot)
    if (existing) return existing
    const key = crypto.randomUUID(); sessionStorage.setItem(slot, key); return key
  } catch {
    if (!fallbackKeys.has(slot)) fallbackKeys.set(slot, crypto.randomUUID())
    return fallbackKeys.get(slot)!
  }
}

export const ticketCategories = { DELIVERY_DELAY: '物流催办', PAYMENT_QUERY: '支付疑问', AFTERSALE_DISPUTE: '售后争议', INFO_VERIFY: '信息核实', COMPLAINT: '投诉建议' } as const
export const ticketStatuses = { OPEN: '等待处理', WAITING_CUSTOMER: '待补充信息', RESOLVED: '已答复', CLOSED: '已关闭' } as const
export interface SupportTicket { id: string; orderId: string; caseId: string | null; category: keyof typeof ticketCategories; status: keyof typeof ticketStatuses; summary: string; version: number }
export interface TicketEvent { id: string; actor: string; action: string; message: string; createdAt: string }
export function parseSupportTicket(value: unknown): SupportTicket {
  const v = value as SupportTicket
  if (!v || typeof v.id !== 'string' || typeof v.orderId !== 'string' || !(v.caseId === null || typeof v.caseId === 'string')
    || !Object.hasOwn(ticketCategories, v.category) || !Object.hasOwn(ticketStatuses, v.status)
    || typeof v.summary !== 'string' || !Number.isSafeInteger(v.version) || v.version < 0) throw new Error('工单信息不完整，请刷新核对')
  return v
}
export function parseTicketEvent(value: unknown): TicketEvent {
  const v = value as TicketEvent
  if (!v || ['id', 'actor', 'action', 'message', 'createdAt'].some(key => typeof (v as unknown as Record<string, unknown>)[key] !== 'string')
    || !Number.isFinite(Date.parse(v.createdAt))) throw new Error('工单记录不完整，请刷新核对')
  return v
}
